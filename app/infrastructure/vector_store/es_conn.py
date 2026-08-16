import asyncio
import json
import os
import time
from typing import Any, Optional
import logging
import copy
import re
from elasticsearch import AsyncElasticsearch, NotFoundError, ConnectionTimeout
from elasticsearch.exceptions import RequestError
from elasticsearch_dsl import Q, Search
from .base import (
    VectorStoreConnection, 
    SearchRequest, 
    MatchTextExpr, 
    MatchDenseExpr, 
    FusionExpr, 
    SortOrder
)
from .utils import get_float, is_english

# 重试次数常量
ATTEMPT_TIME = 3
RETRY_DELAY = 2  # 重试间隔（秒）
CONNECTION_TIMEOUT = 10  # 连接超时（秒）
REQUEST_TIMEOUT = 30     # 请求超时（秒）


class ESConnection(VectorStoreConnection):
    """Elasticsearch连接 - 纯基础设施实现"""

    def __init__(self, hosts: str, username: str = None, password: str = None, mapping_name: str = None, verify_certs: bool = False):
        """
        初始化ES连接
        Args:
            hosts: ES主机地址
            username: 用户名
            password: 密码
            mapping_name: 映射配置文件路径
            verify_certs: 是否校验服务端 TLS 证书，False 时连接 https 会触发库内 SecurityWarning
        """
        self.hosts = hosts
        self.username = username
        self.password = password
        self.verify_certs = verify_certs
        self.es = None
        self.info = None
        self.mapping = None
        self._last_health_check: float = 0
        self._health_check_interval: int = 30
        self._connection_lock = None
        
        self._load_mapping(mapping_name)
        
        logging.info(f"Elasticsearch {self.hosts} initialized.")
    
    def get_db_type(self) -> str:
        """
        获取数据库类型
        Returns:
            str: 数据库类型名称
        """
        return "elasticsearch"    

    async def create_space(self, space_name: str, vector_size: int, **kwargs) -> bool:
        """
        创建索引
        Args:
            space_name: 空间名称
            vector_size: 向量维度大小
            **kwargs: 其他参数
        Returns:
            bool: 创建成功返回True，失败返回False
        """
        await self._ensure_connect()
        
        if await self.space_exists(space_name):
            return True
        
        try:
            await self.es.indices.create(
                index=space_name,
                settings=self.mapping["settings"],
                mappings=self.mapping["mappings"]
            )
            logging.info(f"Created space: {space_name}")
            return True
        except RequestError as e:
            if "resource_already_exists_exception" in str(e):
                logging.info(f"Space already exists: {space_name}")
                return True
            logging.error(f"Failed to create space {space_name}: {e}")
            return False
        except Exception as e:
            logging.error(f"Failed to create space {space_name}: {e}")
            return False

    async def delete_space(self, space_name: str, **kwargs) -> bool:
        """
        删除索引
        Args:
            space_name: 空间名称
            **kwargs: 其他参数
        Returns:
            bool: 删除成功返回True，失败返回False
        """
        await self._ensure_connect()
        
        try:
            await self.es.indices.delete(index=space_name, allow_no_indices=True)
            logging.info(f"Deleted space: {space_name}")
            return True
        except NotFoundError:  
            logging.warning(f"Space {space_name} not found")
            return True
        except Exception as e:
            logging.error(f"Failed to delete space {space_name}: {e}")
            return False

    async def space_exists(self, space_name: str, **kwargs) -> bool:
        """
        检查索引是否存在
        Args:
            space_name: 空间名称
            **kwargs: 其他参数
        Returns:
            bool: 空间存在返回True，不存在返回False
        """
        await self._ensure_connect()
        
        try:
            return await self.es.indices.exists(index=space_name)
        except Exception as e:
            logging.error(f"Failed to check space existence {space_name}: {e}")
            return False

    async def insert_records(self, space_name: str, records: list[dict[str, Any]], **kwargs) -> list[str]:
        """ 
        批量插入数据记录
        Args:
            space_name: 空间名称
            records: 要插入的记录列表，每个记录必须包含id字段
            **kwargs: 其他参数
        Returns:
            list[str]: 插入失败的记录ID列表，成功时返回空列表
        """
        if not records:
            return []
        
        await self._ensure_connect()
        
        try:
            bulk_data = []
            for record in records:
                assert "_id" not in record
                assert "id" in record

                # 构建ES记录
                es_record = copy.deepcopy(record)
                meta_id = es_record.pop("id", "")                
                bulk_data.append({
                    "index": {
                        "_index": space_name, 
                        "_id": meta_id
                    }
                })
                bulk_data.append(es_record)
            
            # 执行批量插入，带重试
            for attempt in range(ATTEMPT_TIME):
                try:
                    r = await self.es.bulk(index=(space_name), operations=bulk_data,
                                                refresh=True, timeout=f"{REQUEST_TIMEOUT}s")
                    
                    failed_records = []
                    if r["errors"]:  # 如果有错误，收集错误信息
                        for item in r["items"]:
                            for action in ["create", "delete", "index", "update"]:
                                if action in item and "error" in item[action]:
                                    failed_records.append(str(item[action]["_id"]) + ":" + str(item[action]["error"]))
                    return failed_records
                
                except Exception as e:
                    if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                        logging.warning(f"批量插入失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    else:
                        logging.error(f"批量插入最终失败: {e}")
                        return [str(e)]
            
            return []
        except Exception as e:
            logging.error(f"Failed to insert records to {space_name}: {e}")
            return []

    async def update_records(self, space_name: str, condition: dict, new_value: dict, fields_to_remove: list[str] = None, **kwargs) -> bool:
        """
        根据条件更新数据记录
        Args:
            space_name: 空间名称
            condition: 更新条件，支持id、exists、terms、term等查询条件
            new_value: 新的字段值，支持remove、add等特殊操作
            fields_to_remove: 需要先删除的字段列表
            **kwargs: 其他参数
        Returns:
            bool: 更新成功返回True，失败返回False
        """
        await self._ensure_connect()
        
        doc = copy.deepcopy(new_value)
        doc.pop("id", None)
        
        # 检查是否是单个文档更新（通过id）
        if "id" in condition and isinstance(condition["id"], str):
            # 更新特定单个文档
            chunk_id = condition["id"]
            try:
                # 先删除指定的字段
                if fields_to_remove:
                    for field_name in fields_to_remove:
                        try:
                            await self.es.update(index=space_name, id=chunk_id, script=f"ctx._source.remove(\"{field_name}\");")
                        except Exception:
                            logging.exception(f"update_records(index={space_name}, id={chunk_id}, doc={json.dumps(condition, ensure_ascii=False)}) got exception")
                
                # 执行更新
                await self.es.update(index=space_name, id=chunk_id, doc=doc)
                return True
            except Exception as e:
                logging.error(f"update_records(index={space_name}, id={chunk_id}) failed: {e}")
                return False

        # 更新多个文档（根据条件）
        bqry = Q("bool")
        for k, v in condition.items():
            if not isinstance(k, str) or not v:
                continue
            if k == "exists":
                bqry.filter.append(Q("exists", field=v))
                continue
            if isinstance(v, list):
                bqry.filter.append(Q("terms", **{k: v}))
            elif isinstance(v, str) or isinstance(v, int):
                bqry.filter.append(Q("term", **{k: v}))
            else:
                raise Exception(f"Condition `{str(k)}={str(v)}` value type is {str(type(v))}, expected to be int, str or list.")
        
        # 构建更新脚本
        scripts = []
        params = {}
        for k, v in new_value.items():
            if k == "remove":
                if isinstance(v, str):
                    scripts.append(f"ctx._source.remove('{v}');")
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        scripts.append(f"int i=ctx._source.{kk}.indexOf(params.p_{kk});ctx._source.{kk}.remove(i);")
                        params[f"p_{kk}"] = vv
                continue
            if k == "add":
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        scripts.append(f"ctx._source.{kk}.add(params.pp_{kk});")
                        params[f"pp_{kk}"] = vv.strip()
                continue
            if (not isinstance(k, str) or not v) and k != "available_int":
                continue
            if isinstance(v, str):
                v = re.sub(r"(['\n\r]|\\.)", " ", v)
                params[f"pp_{k}"] = v
                scripts.append(f"ctx._source.{k}=params.pp_{k};")
            elif isinstance(v, int) or isinstance(v, float):
                scripts.append(f"ctx._source.{k}={v};")
            elif isinstance(v, list):
                scripts.append(f"ctx._source.{k}=params.pp_{k};")
                params[f"pp_{k}"] = json.dumps(v, ensure_ascii=False)
            else:
                raise Exception(f"newValue `{str(k)}={str(v)}` value type is {str(type(v))}, expected to be int, str.")
        
        # 执行批量更新，带重试
        for attempt in range(ATTEMPT_TIME):
            try:
                await self.es.update_by_query(
                    index=space_name,
                    body={
                        "query": bqry.to_dict(),
                        "script": {
                            "source": "".join(scripts),
                            "params": params
                        }
                    },
                    refresh=True,
                    slices=5,
                    conflicts="proceed"
                )
                return True
            except Exception as e:
                if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                    logging.warning(f"批量更新失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logging.error(f"批量更新最终失败: {e}")
                    return False
        return False

    async def delete_records(self, space_name: str, condition: dict, **kwargs) -> int:
        """
        根据条件删除数据记录
        Args:
            space_name: 空间名称
            condition: 删除条件，支持id、exists、terms、term等查询条件
            **kwargs: 其他参数
        Returns:
            int: 删除的记录数量
        """
        await self._ensure_connect()
        
        qry = None
        assert "_id" not in condition
        
        if "id" in condition:
            chunk_ids = condition["id"]
            if not isinstance(chunk_ids, list):
                chunk_ids = [chunk_ids]
            if not chunk_ids:  
                qry = Q("match_all")
            else:
                qry = Q("ids", values=chunk_ids)
        else:
            qry = Q("bool")
            for k, v in condition.items():
                if k == "exists":
                    qry.filter.append(Q("exists", field=v))
                elif k == "must_not":
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            if kk == "exists":
                                qry.must_not.append(Q("exists", field=vv))
                elif isinstance(v, list):
                    qry.must.append(Q("terms", **{k: v}))
                elif isinstance(v, str) or isinstance(v, int):
                    qry.must.append(Q("term", **{k: v}))
                else:
                    raise ValueError("Condition value must be int, str or list.")
        
        logging.debug(f"delete_records query: {json.dumps(qry.to_dict())}")

        try:
            res = await self.es.delete_by_query(
                index=space_name,
                body=Search().query(qry).to_dict(),
                refresh=True)
            # ES 8.x 返回结果格式可能不同，需要兼容处理
            if isinstance(res, dict):
                return res.get("deleted", 0)
            else:
                # 如果是 ObjectApiResponse 对象，尝试转换为字典
                res_dict = dict(res) if hasattr(res, '__iter__') else {}
                return res_dict.get("deleted", 0)
        except Exception as e:
            logging.warning(f"delete_records got exception: {str(e)}")
            return 0

    async def get_record(self, space_names: list[str], record_id: str, **kwargs) -> Optional[dict[str, Any]]:
        """
        获取单个数据记录
        Args:
            space_names: 空间名称列表
            record_id: 记录ID
            **kwargs: 其他参数
        Returns:
            Optional[dict[str, Any]]: 记录数据，不存在时返回None
        """

        if not space_names or len(space_names) > 1:
            logging.error(f"get_record space_names: {space_names} is invalid")
            return None
        
        await self._ensure_connect()
        
        # 调用点只携带个单元
        space_name = space_names[0]
        for attempt in range(ATTEMPT_TIME):
            try:
                response = await self.es.get(index=space_name, id=record_id, source=True)

                if str(response.get("timed_out", "")).lower() == "true":
                    raise Exception("ES Timeout.")
                
                source = response["_source"]
                source["id"] = record_id
                return source
            except NotFoundError:
                return None
            except Exception as e:
                if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                    logging.warning(f"get_record失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logging.error(f"get_record最终失败: {e}")
                    raise e
        
        logging.error(f"get_record timeout for {ATTEMPT_TIME} times!")
        raise Exception("get_record timeout.")

    async def search(self, space_names: list[str], request: SearchRequest, **kwargs) -> dict[str, Any]:
        """
        搜索数据记录
        Args:
            space_names: 空间名称列表
            request: 搜索请求对象，包含查询条件、分页、排序等信息
            **kwargs: 其他参数
        Returns:
            dict[str, Any]: 搜索结果，包含hits、total、aggregations等
        """
        try:
            if not space_names:
                logging.error(f"search: space_names is invalid")
                return None

            await self._ensure_connect()

            # 判断条件中是否包含_id
            assert "_id" not in request.condition

            # 构建布尔查询
            bqry = Q("bool", must=[])            
            # 添加过滤条件
            if request.condition:
                for field, value in request.condition.items():
                    if field == "available_int":
                        if value == 0:
                            bqry.filter.append(Q("range", available_int={"lt": 1}))
                        else:
                            bqry.filter.append(Q("bool", must_not=Q("range", available_int={"lt": 1})))
                        continue
                    if not value:
                        continue
                    if isinstance(value, list):
                        bqry.filter.append(Q("terms", **{field: value}))
                    elif isinstance(value, (str, int)):
                        bqry.filter.append(Q("term", **{field: value}))
                    else:
                        raise Exception(f"Condition `{str(field)}={str(value)}` value type is {str(type(value))}, expected to be int, str or list.")

            # 添加文本搜索
            search = Search()
            vector_similarity_weight = 0.5
            if request.match_exprs:
                for match_expr in request.match_exprs:
                    if isinstance(match_expr, FusionExpr) and match_expr.method == "weighted_sum" and "weights" in match_expr.fusion_params:
                        assert len(request.match_exprs) == 3 and isinstance(request.match_exprs[0], MatchTextExpr) and isinstance(
                            request.match_exprs[1], MatchDenseExpr) and isinstance(request.match_exprs[2], FusionExpr)
                        weights = match_expr.fusion_params["weights"]
                        vector_similarity_weight = get_float(weights.split(",")[1])

                for match_expr in request.match_exprs:
                    if isinstance(match_expr, MatchTextExpr):
                        minimum_should_match = match_expr.extra_options.get("minimum_should_match", 0.0)
                        if isinstance(minimum_should_match, float):
                            minimum_should_match = str(int(minimum_should_match * 100)) + "%"
                        bqry.must.append(Q("query_string", fields=match_expr.fields,
                                        type="best_fields", query=match_expr.matching_text,
                                        minimum_should_match=minimum_should_match,
                                        boost=1))
                        bqry.boost = 1.0 - vector_similarity_weight
                    elif isinstance(match_expr, MatchDenseExpr):
                        assert (bqry is not None)
                        similarity = 0.0
                        if "similarity" in match_expr.extra_options:
                            similarity = match_expr.extra_options["similarity"]
                        search = search.knn(match_expr.vector_column_name,
                                match_expr.topn,
                                match_expr.topn * 2,
                                query_vector=list(match_expr.embedding_data),
                                filter=bqry.to_dict(),
                                similarity=similarity,
                            )
            
            # 添加排名特征
            if request.rank_feature and bqry:
                for field, score in request.rank_feature.fields.items():
                    if field not in request.rank_feature.exclude_fields:
                        field = f"{request.rank_feature.field_prefix}.{field}"
                    bqry.should.append(Q("rank_feature", field=field, linear={}, boost=score))

            # 应用查询
            if bqry:
                search = search.query(bqry)
            
            # 添加高亮
            if request.highlight_fields:
                for field in request.highlight_fields:
                    search = search.highlight(field)
            
            # 添加排序
            if request.order_by:
                orders = []

                for sort_field in request.order_by:
                    order_info = {"order": "asc" if sort_field.sort_order == SortOrder.ASC else "desc"}
                    
                    # 根据字段类型和配置添加排序参数
                    if sort_field.sort_unmapped_type:
                        order_info["unmapped_type"] = sort_field.sort_unmapped_type
                    if sort_field.sort_mode:
                        order_info["mode"] = sort_field.sort_mode
                    if sort_field.sort_numeric_type:
                        order_info["numeric_type"] = sort_field.sort_numeric_type
                    
                    orders.append({sort_field.sort_field: order_info})
                
                search = search.sort(*orders)
            
            # 添加聚合
            if request.agg_fields:
                for field in request.agg_fields:
                    search.aggs.bucket(f'aggs_{field}', 'terms', field=field, size=1000000)
            
            # 设置分页
            if request.limit > 0:
                search = search[request.offset:request.offset + request.limit]
            
            query = search.to_dict()
            query["timeout"] = f"{REQUEST_TIMEOUT}s"
            logging.debug(f"search {str(space_names)} query: " + json.dumps(query))

            # 执行搜索
            for attempt in range(ATTEMPT_TIME):
                try:
                    result = await self.es.search(
                        index=space_names,
                        body=query,
                        track_total_hits=True,
                        _source=True)

                    if str(result.get("timed_out", "")).lower() == "true":
                        raise Exception("Es Timeout.")

                    # ES 8.x 返回的是 ObjectApiResponse 对象，需要转换为字典才能序列化
                    result_dict = dict(result) if hasattr(result, '__iter__') else result
                    logging.debug(f"search {str(space_names)} result: " + json.dumps(result_dict))
                    return result
                except Exception as e:
                    if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                        logging.warning(f"搜索失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    else:
                        logging.error(f"搜索最终失败: {e}")
                        raise e
        
        except Exception as e:
            logging.error(f"search {str(space_names)} query: " + json.dumps(query) + str(e))
            raise e
        
        logging.error(f"search timeout for {ATTEMPT_TIME} times!")
        raise Exception("search timeout.")

    """
    Helper functions for search result
    """
    # 获取总数
    def get_total(self, result) -> int:
        """
        获取搜索结果总数
        Args:
            result: 搜索结果对象
        Returns:
            int: 搜索结果总数
        """
        try:
            if "hits" in result and "total" in result["hits"]:
                total_count = result["hits"]["total"]
                if isinstance(total_count, dict):
                    return total_count.get("value", 0)
                return total_count
            return 0
        except Exception as e:
            logging.error(f"get_total error: {str(e)}")
            return 0

    # 获取Chunk IDs
    def get_chunk_ids(self, result) -> list[str]:
        """
        获取搜索结果中的Chunk IDs
        Args:
            result: 搜索结果对象
        Returns:
            list[str]: Chunk ID列表
        """
        try:
            chunk_ids = []
            if "hits" in result and "hits" in result["hits"]:
                for hit in result["hits"]["hits"]:
                    if "_id" in hit:
                        chunk_ids.append(hit["_id"])
            return chunk_ids
        except Exception as e:
            logging.error(f"get_chunk_ids error: {str(e)}")
            return []

    # 获取Fields
    def get_source(self, result) -> list[dict[str, Any]]:
        """
        获取搜索结果中的_source数据，并添加id和_score字段
        Args:
            result: 搜索结果对象
        Returns:
            list[dict]: 包含_source数据的列表，每个元素添加了id和_score字段
        """
        sources = []
        for hit in result["hits"]["hits"]:
            hit["_source"]["id"] = hit["_id"]
            hit["_source"]["_score"] = hit["_score"]
            sources.append(hit["_source"])
        return sources

    def get_fields(self, result, fields: list[str]) -> dict[str, Any]:
        """
        获取搜索结果中指定字段的数据
        Args:
            result: 搜索结果对象
            fields: 需要获取的字段名列表
        Returns:
            dict[str, dict]: 字段数据字典，key为字段名，value为字段值
        """
        try:
            field_data = {}
            if not fields:
                return {}
            for source in self.get_source(result):
                data = {name: source.get(name) for name in fields if source.get(name) is not None}
                for name, value in data.items():
                    if isinstance(value, list):
                        data[name] = value
                    elif name == "available_int" and isinstance(value, (int, float)):
                        data[name] = value
                    elif not isinstance(value, str):
                        data[name] = str(data[name])
                    # if name.find("tks") > 0:
                    #     data[name] = rmSpace(data[name])

                if data:
                    field_data[source["id"]] = data
            return field_data
        except Exception as e:
            logging.error(f"get_fields error: {str(e)}")
            return {}

    # 获取Highlight
    def get_highlight(self, result, keywords: list[str], field_name: str) -> dict[str, Any]:
        """
        获取搜索结果中的高亮信息
        Args:
            result: 搜索结果对象
            keywords: 关键词列表
            fieldnm: 字段名
        Returns:
            dict[str, str]: 高亮信息字典，key为文档ID，value为高亮文本
        """
        try:
            highlight_data = {}
            for hit in result["hits"]["hits"]:
                highlights = hit.get("highlight")
                if not highlights:
                    continue
                highlight_text = "...".join([text for text in list(highlights.items())[0][1]])
                if not is_english(highlight_text.split()):
                    highlight_data[hit["_id"]] = highlight_text
                    continue

                source_text = hit["_source"][field_name]
                source_text = re.sub(r"[\r\n]", " ", source_text, flags=re.IGNORECASE | re.MULTILINE)
                highlighted_sentences = []
                for sentence in re.split(r"[.?!;\n]", source_text):
                    for keyword in keywords:
                        sentence = re.sub(r"(^|[ .?/'\"\(\)!,:;-])(%s)([ .?/'\"\(\)!,:;-])" % re.escape(keyword), r"\1<em>\2</em>\3", sentence,
                                   flags=re.IGNORECASE | re.MULTILINE)
                    if not re.search(r"<em>[^<>]+</em>", sentence, flags=re.IGNORECASE | re.MULTILINE):
                        continue
                    highlighted_sentences.append(sentence)
                highlight_data[hit["_id"]] = "...".join(highlighted_sentences) if highlighted_sentences else "...".join([text for text in list(highlights.items())[0][1]])

            return highlight_data
        except Exception as e:
            logging.error(f"get_highlight error: {str(e)}")
            return {}

    # 获取Aggregation
    def get_aggregation(self, result, field_name: str) -> dict[str, Any]:
        """
        获取搜索结果中的聚合信息
        Args:
            result: 搜索结果对象
            field_name: 聚合字段名
        Returns:
            list[tuple]: 聚合结果列表，每个元素为(key, doc_count)元组
        """
        try:
            agg_field = "aggs_" + field_name
            if "aggregations" not in result or agg_field not in result["aggregations"]:
                return []
            buckets = result["aggregations"][agg_field]["buckets"]
            return [(bucket["key"], bucket["doc_count"]) for bucket in buckets]
        except Exception as e:
            logging.error(f"get_aggregation error: {str(e)}")
            return []

    """
    SQL
    """
    # 执行SQL
    async def sql(self, sql: str, fetch_size: int, format: str, tokenize_func=None, fine_grained_tokenize_func=None):
        """
        执行由text-to-sql生成的SQL查询
        Args:
            sql: SQL查询语句
            fetch_size: 获取记录数量
            format: 返回格式
            tokenize_func: 基础分词函数，用于对查询值进行分词处理，如果为None则不分词
            fine_grained_tokenize_func: 细粒度分词函数，用于对已分词的内容进行进一步处理，如果为None则不分词
        Returns:
            查询结果
        """
        await self._ensure_connect()
        
        logging.debug(f"sql: {sql}")
        
        # SQL预处理：清理格式
        processed_sql = re.sub(r"[ `]+", " ", sql)
        processed_sql = processed_sql.replace("%", "")
        
        # 处理token字段的查询转换
        token_replacements = []
        for match in re.finditer(r" ([a-z_]+_l?tks)( like | ?= ?)'([^']+)'", processed_sql):
            field_name, operator, value = match.group(1), match.group(2), match.group(3)
            
            # 如果提供了分词函数，则进行分词处理；否则直接使用原值
            if tokenize_func and fine_grained_tokenize_func:
                tokenized_value = fine_grained_tokenize_func(tokenize_func(value))
            else:
                tokenized_value = value
            match_query = f" MATCH({field_name}, '{tokenized_value}', 'operator=OR;minimum_should_match=30%') "
            
            token_replacements.append((f"{field_name}{operator}'{value}'", match_query))

        # 应用token字段替换
        for original_pattern, replacement in token_replacements:
            processed_sql = processed_sql.replace(original_pattern, replacement, 1)
        
        logging.debug(f"sql to es: {processed_sql}")

        # 执行查询，带重试机制
        for attempt in range(ATTEMPT_TIME):
            try:
                result = await self.es.sql.query(
                    body={"query": processed_sql, "fetch_size": fetch_size}, 
                    format=format,
                    request_timeout=f"{REQUEST_TIMEOUT}s"
                )
                return result
            except Exception as e:
                if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                    logging.warning(f"SQL查询失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logging.error(f"SQL查询最终失败: {e}")
                    return None
        
        logging.error(f"sql timeout for {ATTEMPT_TIME} times!")
        return None
    
    async def health_check(self) -> bool:
        """
        健康检查
        Returns:
            bool: 健康状态，True表示健康，False表示不健康
        """      
        await self._ensure_connect()

        try: 
            result = await self.es.ping()
            logging.debug(f"Elasticsearch健康检查: ping={result}")
            return result
        except Exception as e:
            logging.error(f"Elasticsearch健康检查失败: {e}")
            return False

    async def close(self):
        """
        关闭Elasticsearch连接
        """
        try:
            if self.es:
                await self.es.close()
                self.es = None
                self.info = None
                logging.info("Elasticsearch连接已关闭")
        except Exception as e:
            logging.warning(f"关闭Elasticsearch连接时出错: {e}")
        finally:
            self.es = None
            self.info = None

    def _load_mapping(self, mapping_name: str):
        """
        加载映射配置
        Args:
            mapping_name: 映射配置文件名
        """
        if mapping_name:
            # 取当前文件所在目录下的mapping目录路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            mapping_dir = os.path.join(current_dir, "mapping")
            mapping_config_path = os.path.join(mapping_dir, mapping_name)

            if os.path.exists(mapping_config_path):
                with open(mapping_config_path, 'r', encoding='utf-8') as f:
                    self.mapping = json.load(f)

                    logging.info(f"Loaded mapping from {mapping_config_path}")
                    return

        # 默认映射配置
        self.mapping = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0
            },
            "mappings": {
                "properties": {
                    "content": {"type": "text"},
                    "vector": {"type": "dense_vector", "dims": 768}
                }
            }
        }
        logging.info("Using default mapping configuration")
    
    async def _ensure_connect(self):
        """
        确保连接已建立且健康 - 供业务方法调用
        """
        # 1. 检查连接是否存在
        if self.es is None:
            if self._connection_lock is None:
                self._connection_lock = asyncio.Lock()
            async with self._connection_lock:
                if self.es is None:  # 双重检查锁定
                    await self._connect()
        
        # 2. 检查连接是否健康
        if self._should_check_health():
            try:
                if not await self.es.ping():
                    logging.warning("ES连接不健康，重新连接")
                    if self._connection_lock is None:
                        self._connection_lock = asyncio.Lock()
                    async with self._connection_lock:
                        await self._connect()  # 重新连接
            except Exception as e:
                logging.warning(f"ES连接健康检查失败: {e}，重新连接")
                if self._connection_lock is None:
                    self._connection_lock = asyncio.Lock()
                async with self._connection_lock:
                    await self._connect()  # 重新连接
            self._last_health_check = time.time()

    async def _connect(self):
        """
        建立ES连接
        """
        # 先关闭现有连接
        if self.es:
            try:
                await self.es.close()
            except:
                pass
            self.es = None
        
        try:
            for attempt in range(ATTEMPT_TIME):   
                try:        
                    # 异步连接
                    self.es = AsyncElasticsearch(
                        self.hosts.split(","),
                        basic_auth=(self.username, self.password) if self.username and self.password else None,
                        verify_certs=self.verify_certs,
                        timeout=CONNECTION_TIMEOUT,
                        max_retries=3
                    )

                    if self.es and await self.es.ping():
                        self.info = await self.es.info()
                        logging.info(f"Connected to Elasticsearch {self.hosts}")
                        break  # 连接成功，跳出循环
                    else:
                        logging.warning(f"Elasticsearch {self.hosts} 连接失败，等待重试...")
                
                except asyncio.CancelledError:
                    logging.error(f"ES连接被取消: {self.hosts}")
                    raise
                except Exception as e:
                    logging.warning(f"Elasticsearch {self.hosts} 连接异常: {e}")

                if attempt < ATTEMPT_TIME - 1:  # 不是最后一次尝试
                    await asyncio.sleep(RETRY_DELAY)
            else:
                # 如果所有重试都失败了
                msg = f"Elasticsearch {self.hosts} 连接失败，已尝试 {ATTEMPT_TIME} 次"
                logging.error(msg)
                raise ConnectionError(msg)

        except asyncio.CancelledError:
            logging.error(f"ES连接被取消: {self.hosts}")
            raise
        except Exception as e:
            logging.error(f"Failed to connect to Elasticsearch: {e}")
            raise

        # 连接成功后检查ES版本
        version_info = self.info.get("version", {"number": "8.11.3"})
        version = version_info["number"].split(".")[0]
        if int(version) < 8:
            msg = f"Elasticsearch version must be greater than or equal to 8, current version: {version}"
            logging.error(msg)
            raise Exception(msg)

    def _should_check_health(self) -> bool:
        """判断是否需要健康检查"""
        return time.time() - self._last_health_check > self._health_check_interval

    def _should_retry(self, error: Exception) -> bool:
        """判断是否应该重试"""
        error_str = str(error).lower()
        return any(keyword in error_str for keyword in [
            'timeout', 'connection', 'network', 'temporary', 'overload', '429'
        ])