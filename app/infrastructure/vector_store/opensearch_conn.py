import asyncio
import json
import os
from typing import Any, Optional
import logging
import copy
import re
import time
from opensearchpy import OpenSearch, NotFoundError, ConnectionTimeout, UpdateByQuery, Q, Search, Index
from opensearchpy.client import IndicesClient
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

class OSConnection(VectorStoreConnection):
    """OpenSearch连接 - 纯基础设施实现"""

    def __init__(self, hosts: str, username: str = None, password: str = None, mapping_name: str = None):
        """
        初始化OpenSearch连接
        Args:
            hosts: OpenSearch主机地址
            username: 用户名
            password: 密码
            mapping_name: 映射配置文件路径
        """
        self.hosts = hosts
        self.username = username
        self.password = password
        self.os = None
        self.info = None
        self.mapping = None
        self._last_health_check: float = 0
        self._health_check_interval: int = 30
        self._connection_lock = None
        
        self._load_mapping(mapping_name)
        
        logging.info(f"OpenSearch {self.hosts} initialized.")
    
    def get_db_type(self) -> str:
        """
        获取数据库类型
        Returns:
            str: 数据库类型名称
        """
        return "opensearch"

    async def create_space(self, space_name: str, vector_size: int, **kwargs) -> bool:
        """
        创建索引
        在OpenSearch中创建新的索引，包含向量搜索配置
        Args:
            space_name: 索引名称
            vector_size: 向量维度大小
            **kwargs: 其他参数
        Returns:
            bool: 创建成功返回True，失败返回False
        """
        await self._ensure_connect()
        
        if await self.space_exists(space_name):
            return True
        
        try: 
            await asyncio.to_thread(
                lambda: IndicesClient(self.os).create(index=space_name, body=self.mapping)
            )
            logging.info(f"Created space: {space_name}")
            return True
        except Exception as e:
            logging.error(f"Failed to create space {space_name}: {e}")
            return False

    async def delete_space(self, space_name: str, **kwargs) -> bool:
        """
        删除索引
        删除指定的OpenSearch索引及其所有数据
        Args:
            space_name: 索引名称
            **kwargs: 其他参数
        Returns:
            bool: 删除成功返回True，失败返回False
        """
        await self._ensure_connect()
        
        try:
            await asyncio.to_thread(
                lambda: self.os.indices.delete(index=space_name, allow_no_indices=True)
            )
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
        验证指定的OpenSearch索引是否存在
        Args:
            space_name: 索引名称
            **kwargs: 其他参数
        Returns:
            bool: 索引存在返回True，不存在返回False
        """
        await self._ensure_connect()
        
        s = Index(space_name, self.os)
        try:
            return await asyncio.to_thread(s.exists)
        except Exception as e:
            logging.error(f"检查索引存在失败: {e}")
            return False

    async def insert_records(self, space_name: str, records: list[dict[str, Any]], **kwargs) -> list[str]:
        """
        批量插入文档
        将文档记录批量插入到OpenSearch索引中
        Args:
            space_name: 索引名称
            records: 要插入的文档记录列表
            **kwargs: 其他参数
        Returns:
            list[str]: 插入失败的记录ID列表，成功时返回空列表
        """
        await self._ensure_connect()
        
        if not records:
            return []
        
        operations = []
        for document in records:
            assert "_id" not in document
            assert "id" in document
            document_copy = copy.deepcopy(document)
            meta_id = document_copy.pop("id", "")
            operations.append(
                {"index": {"_index": space_name, "_id": meta_id}})
            operations.append(document_copy)

        for attempt in range(ATTEMPT_TIME):
            try:
                response = await asyncio.to_thread(
                    lambda: self.os.bulk(index=(space_name), body=operations, refresh=True, timeout=f"{REQUEST_TIMEOUT}s")
                )
                
                failed_records = []
                if response["errors"]:  # 如果有错误，收集错误信息
                    for item in response["items"]:
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
        
        return []  # Should not be reached if exceptions are raised or retries exhausted

    async def update_records(self, space_name: str, condition: dict, new_value: dict, fields_to_remove: list[str] = None, **kwargs) -> bool:
        """
        根据条件更新数据记录
        根据指定条件更新OpenSearch索引中的文档记录
        Args:
            space_name: 空间名称（索引名）
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
            for attempt in range(ATTEMPT_TIME):
                try:
                    # 先删除指定的字段
                    if fields_to_remove:
                        for field_name in fields_to_remove:
                            try:
                                await asyncio.to_thread(
                                    lambda: self.os.update(index=space_name, id=chunk_id, body={"script": f"ctx._source.remove(\"{field_name}\");"})
                                )
                            except Exception:
                                logging.warning(f"Failed to remove field {field_name} from document {chunk_id}")
                    
                    # 执行更新
                    await asyncio.to_thread(
                        lambda: self.os.update(index=space_name, id=chunk_id, body=doc)
                    )
                    return True
                except Exception as e:
                    if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                        logging.warning(f"更新单个文档失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                        await asyncio.sleep(RETRY_DELAY)
                        continue
                    else:
                        logging.error(f"更新单个文档最终失败: {e}")
                        return False
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
        
        # 执行批量更新
        ubq = UpdateByQuery(index=space_name).using(self.os).query(bqry)
        ubq = ubq.script(source="".join(scripts), params=params)
        ubq = ubq.params(refresh=True)
        ubq = ubq.params(slices=5)
        ubq = ubq.params(conflicts="proceed")

        for attempt in range(ATTEMPT_TIME):
            try:
                _ = await asyncio.to_thread(ubq.execute)
                return True
            except Exception as e:
                if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                    logging.warning(f"更新记录失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logging.error(f"更新记录最终失败: {e}")
                    return False
        return False

    async def delete_records(self, space_name: str, condition: dict, **kwargs) -> int:
        """
        根据条件删除数据记录
        根据指定条件删除OpenSearch索引中的文档记录
        Args:
            space_name: 索引名称
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
        
        logging.debug(f"delete query: {json.dumps(qry.to_dict())}")
        for attempt in range(ATTEMPT_TIME):
            try:
                res = await asyncio.to_thread(
                    lambda: self.os.delete_by_query(
                        index=space_name,
                        body=Search().query(qry).to_dict(),
                        refresh=True
                    )
                )
                return res["deleted"]
            except Exception as e:
                if re.search(r"(not_found)", str(e), re.IGNORECASE):
                    return 0
                
                if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                    logging.warning(f"删除记录失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logging.error(f"删除记录最终失败: {e}")
                    return 0
        return 0

    async def get_record(self, space_names: list[str], record_id: str, **kwargs) -> Optional[dict[str, Any]]:
        """
        获取单个数据记录
        根据记录ID从OpenSearch索引中获取单个文档记录
        Args:
            space_names: 索引名称列表
            record_id: 记录ID
            **kwargs: 其他参数
        Returns:
            Optional[dict[str, Any]]: 记录数据，不存在时返回None
        """
        await self._ensure_connect()
        
        if not space_names or len(space_names) > 1:
            logging.error(f"get_record space_names: {space_names} is invalid")
            return None
        
        # 调用点只携带个单元
        space_name = space_names[0]
        for attempt in range(ATTEMPT_TIME):
            try:
                response = await asyncio.to_thread(
                    lambda: self.os.get(index=space_name, id=record_id, source=True)
                )

                if str(response.get("timed_out", "")).lower() == "true":
                    raise Exception("get_record timeout.")
                
                source = response["_source"]
                source["id"] = record_id
                return source
            except NotFoundError:
                return None
            except Exception as e:
                if re.search(r"(not_found)", str(e), re.IGNORECASE):
                    return None
                
                if attempt < ATTEMPT_TIME - 1 and self._should_retry(e):
                    logging.warning(f"获取记录失败，重试 (尝试 {attempt + 1}/{ATTEMPT_TIME}): {e}")
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                else:
                    logging.error(f"获取记录最终失败: {e}")
                    raise Exception(f"get_record failed: {e}")
        
        return None  # Should not be reached

    async def search(self, space_names: list[str], request: SearchRequest, **kwargs) -> dict[str, Any]:
        """
        搜索数据记录
        执行复杂的搜索查询，支持文本搜索、向量搜索和混合搜索
        Args:
            space_names: 索引名称列表
            request: 搜索请求对象，包含查询条件、分页、排序等信息
            **kwargs: 其他参数
        Returns:
            dict[str, Any]: 搜索结果，包含hits、total、aggregations等
        """
        await self._ensure_connect()
        
        try:
            if not space_names:
                logging.error(f"search: space_names is invalid")
                return None

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
            use_knn = False
            knn_query = {}
            
            if request.match_exprs:
                for match_expr in request.match_exprs:
                    if isinstance(match_expr, FusionExpr) and match_expr.method == "weighted_sum" and "weights" in match_expr.fusion_params:
                        assert len(request.match_exprs) == 3 and isinstance(request.match_exprs[0], MatchTextExpr) and isinstance(
                            request.match_exprs[1], MatchDenseExpr) and isinstance(request.match_exprs[2], FusionExpr)
                        weights = match_expr.fusion_params["weights"]
                        vector_similarity_weight = float(weights.split(",")[1])

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
                        use_knn = True
                        vector_column_name = match_expr.vector_column_name
                        knn_query[vector_column_name] = {}
                        knn_query[vector_column_name]["vector"] = list(match_expr.embedding_data)
                        knn_query[vector_column_name]["k"] = match_expr.topn
                        knn_query[vector_column_name]["filter"] = bqry.to_dict()
                        knn_query[vector_column_name]["boost"] = similarity
            
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
                    search = search.highlight(field, force_source=True, no_match_size=30, require_field_match=False)
            
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
            
            # 如果使用KNN，替换query
            if use_knn:
                del query["query"]
                query["query"] = {"knn": knn_query}
            
            logging.debug(f"search {str(space_names)} query: " + json.dumps(query))

            # 执行搜索
            for attempt in range(ATTEMPT_TIME):
                try:
                    result = await asyncio.to_thread(
                        lambda: self.os.search(
                            index=space_names, 
                            body=query, 
                            timeout=f"{REQUEST_TIMEOUT}s", 
                            track_total_hits=True, 
                            _source=True
                        )
                    )

                    if str(result.get("timed_out", "")).lower() == "true":
                        raise Exception("OpenSearch Timeout.")

                    logging.debug(f"search {str(space_names)} res: " + str(result))
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

    """
    Helper functions for search result
    """
    # 获取总数
    def get_total(self, result) -> int:
        """
        获取搜索结果总数
        从OpenSearch搜索结果中提取总记录数
        Args:
            result: OpenSearch搜索结果
        Returns:
            int: 搜索结果的总记录数
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
        从OpenSearch搜索结果中提取所有文档的ID列表
        Args:
            result: OpenSearch搜索结果
        Returns:
            list[str]: 文档ID列表
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
        从OpenSearch搜索结果中提取文档源数据
        Args:
            result: OpenSearch搜索结果
        Returns:
            list[dict[str, Any]]: 包含文档源数据的列表
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
        从OpenSearch搜索结果中提取指定字段的数据
        Args:
            result: OpenSearch搜索结果
            fields: 要提取的字段名列表
        Returns:
            dict[str, Any]: 以文档ID为键，字段数据为值的字典
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
        为OpenSearch搜索结果中的关键词添加高亮标记
        Args:
            result: OpenSearch搜索结果
            keywords: 要高亮的关键词列表
            field_name: 要高亮的字段名
        Returns:
            dict[str, Any]: 以文档ID为键，高亮文本为值的字典
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
        从OpenSearch搜索结果中提取聚合统计信息
        Args:
            result: OpenSearch搜索结果
            field_name: 要聚合的字段名
        Returns:
            dict[str, Any]: 聚合结果，包含字段值和文档数量
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
        在OpenSearch中执行SQL查询，支持分词和格式转换
        Args:
            sql: SQL查询语句
            fetch_size: 获取结果数量限制
            format: 返回格式
            tokenize_func: 基础分词函数，用于对查询值进行分词处理，如果为None则不分词
            fine_grained_tokenize_func: 细粒度分词函数，用于对已分词的内容进行进一步处理，如果为None则不分词
        Returns:
            Any: 查询结果，失败时返回None
        """
        logging.debug(f"sql: {sql}")
        
        await self._ensure_connect()
        
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
        
        logging.debug(f"sql to os: {processed_sql}")

        # 执行查询，带重试机制
        for attempt in range(ATTEMPT_TIME):
            try:
                result = await asyncio.to_thread(
                    lambda: self.os.sql.query(
                        body={"query": processed_sql, "fetch_size": fetch_size}, 
                        format=format,
                        request_timeout="2s"
                    )
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
            logging.error(f"OpenSearch检查失败: {e}")
            return False

    
    async def close(self):
        """
        关闭OpenSearch连接
        """
        try:
            if self.os:
                self.os.close()
                self.os = None
                self.info = None
                logging.info("OpenSearch连接已关闭")
        except Exception as e:
            logging.warning(f"关闭OpenSearch连接时出错: {e}")
        finally:
            self.os = None
            self.info = None

    def _load_mapping(self, mapping_name: str):
        """
        加载映射配置
        从映射文件或默认配置中加载索引映射设置
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

        # 默认映射配置（OpenSearch特有设置）
        self.mapping = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "knn": True,  # OpenSearch特有
                    "knn.algo_param.ef_search": 100
                },
                "mappings": {
                    "properties": {
                        "content": {"type": "text"},
                        "vector": {
                            "type": "knn_vector",
                            "dimension": 768,
                            "method": {
                                "name": "hnsw",
                                "space_type": "cosinesimil",
                                "engine": "nmslib",
                                "parameters": {
                                    "ef_construction": 128,
                                    "m": 24
                                }
                            }
                        }
                    }
                }
            }
    
    async def _ensure_connect(self):
        """
        确保连接已建立且健康 - 供业务方法调用
        """
        # 1. 检查连接是否存在
        if self.os is None:
            if self._connection_lock is None:
                self._connection_lock = asyncio.Lock()
            async with self._connection_lock:
                if self.os is None:  # 双重检查锁定
                    await self._connect()
        
        # 2. 检查连接是否健康
        if self._should_check_health():
            try:
                if not await self.os.ping():
                    logging.warning("OpenSearch连接不健康，重新连接")
                    if self._connection_lock is None:
                        self._connection_lock = asyncio.Lock()
                    async with self._connection_lock:
                        await self._connect()  # 重新连接
            except Exception as e:
                logging.warning(f"OpenSearch连接健康检查失败: {e}，重新连接")
                if self._connection_lock is None:
                    self._connection_lock = asyncio.Lock()
                async with self._connection_lock:
                    await self._connect()  # 重新连接
            self._last_health_check = time.time()

    async def _connect(self):
        """
        建立OpenSearch连接
        """
        # 先关闭现有连接
        if self.os:
            try:
                self.os.close()
            except:
                pass
            self.os = None
        
        try:
            for attempt in range(ATTEMPT_TIME):   
                try:        
                    # 异步连接
                    self.os = OpenSearch(
                        self.hosts.split(","),
                        http_auth=(self.username, self.password) if self.username and self.password else None,
                        verify_certs=False,
                        timeout=CONNECTION_TIMEOUT
                    )

                    if self.os and await self.os.ping():
                        self.info = await self.os.info()
                        logging.info(f"Connected to OpenSearch {self.hosts}")
                        break  # 连接成功，跳出循环
                    else:
                        logging.warning(f"OpenSearch {self.hosts} 连接失败，等待重试...")
                
                except asyncio.CancelledError:
                    logging.error(f"OpenSearch连接被取消: {self.hosts}")
                    raise
                except Exception as e:
                    logging.warning(f"OpenSearch {self.hosts} 连接异常: {e}")

                if attempt < ATTEMPT_TIME - 1:  # 不是最后一次尝试
                    await asyncio.sleep(RETRY_DELAY)
            else:
                # 如果所有重试都失败了
                msg = f"OpenSearch {self.hosts} 连接失败，已尝试 {ATTEMPT_TIME} 次"
                logging.error(msg)
                raise ConnectionError(msg)
        
        except asyncio.CancelledError:
            logging.error(f"OpenSearch连接被取消: {self.hosts}")
            raise
        except Exception as e:
            logging.error(f"Failed to connect to OpenSearch: {e}")
            raise

        # 连接成功后检查OpenSearch版本
        version_info = self.info.get("version", {"number": "2.18.0"})
        version = version_info["number"].split(".")[0]
        if int(version) < 2:
            msg = f"OpenSearch version must be greater than or equal to 2, current version: {version}"
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