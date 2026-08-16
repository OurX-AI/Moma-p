import asyncio
import logging
import os
import re
import shutil
from typing import Any, Optional
import lancedb
import pyarrow as pa
from app.infrastructure.vector_store.base import (
    MatchDenseExpr,
    MatchTextExpr,
    SearchRequest,
    VectorStoreConnection,
)


def _escape_sql_string(value: str) -> str:
    return value.replace("'", "''")


def _build_where(condition: Optional[dict[str, Any]]) -> Optional[str]:
    if not condition:
        return None
    parts: list[str] = []
    for field, value in condition.items():
        if value is None or value == "":
            continue
        if field == "id":
            chunk_ids = value if isinstance(value, list) else [value]
            if not chunk_ids:
                continue
            escaped = ", ".join(f"'{_escape_sql_string(str(v))}'" for v in chunk_ids)
            parts.append(f"id IN ({escaped})")
        elif isinstance(value, list):
            escaped = ", ".join(
                f"'{_escape_sql_string(str(v))}'" if isinstance(v, str) else str(v) for v in value
            )
            parts.append(f"{field} IN ({escaped})")
        elif isinstance(value, str):
            parts.append(f"{field} = '{_escape_sql_string(value)}'")
        elif isinstance(value, (int, float, bool)):
            parts.append(f"{field} = {value}")
        else:
            raise ValueError(f"Unsupported condition value type for {field}: {type(value)}")
    if not parts:
        return None
    return " AND ".join(parts)


def _vector_field_name(vector_size: int) -> str:
    return f"q_{vector_size}_vec"


_METADATA_FIELDS: list[tuple[str, pa.DataType]] = [
    ("start_line", pa.int32()),
    ("end_line", pa.int32()),
    ("chunk_index", pa.int32()),
    ("content", pa.string()),
    ("symbol_kind", pa.string()),
    ("symbol_name", pa.string()),
    ("summary", pa.string()),
]


def _build_table_schema(vector_size: int) -> pa.Schema:
    vector_field = _vector_field_name(vector_size)
    fields: list[pa.Field] = [
        pa.field("id", pa.string()),
        pa.field("repo_id", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("analysis_type", pa.string()),
    ]
    for name, dtype in _METADATA_FIELDS:
        fields.append(pa.field(name, dtype, nullable=True))
    fields.append(pa.field(vector_field, pa.list_(pa.float32(), vector_size)))
    return pa.schema(fields)


def _table_schema_compatible(table_schema: pa.Schema, vector_size: int) -> bool:
    names = {field.name for field in table_schema}
    required = {
        "id",
        "repo_id",
        "file_path",
        "analysis_type",
        _vector_field_name(vector_size),
        "start_line",
        "end_line",
        "content",
    }
    return required.issubset(names)


class LanceDBConnection(VectorStoreConnection):
    """LanceDB 本地嵌入式向量存储。"""

    # LanceDB table_names 默认 limit=10；此处翻页拉取，避免漏表。
    TABLE_NAMES_PAGE_SIZE = 1000

    def __init__(self, uri: str):
        self.uri = os.path.abspath(uri)
        os.makedirs(self.uri, exist_ok=True)
        self.db = lancedb.connect(self.uri)
        self._space_dims: dict[str, int] = {}
        self._space_locks: dict[str, asyncio.Lock] = {}
        self._space_locks_guard = asyncio.Lock()
        logging.info("LanceDB initialized at %s", self.uri)

    async def _get_space_lock(self, space_name: str) -> asyncio.Lock:
        async with self._space_locks_guard:
            lock = self._space_locks.get(space_name)
            if lock is None:
                lock = asyncio.Lock()
                self._space_locks[space_name] = lock
            return lock

    def _space_dir(self, space_name: str) -> str:
        return os.path.join(self.uri, f"{space_name}.lance")

    def _space_dir_exists(self, space_name: str) -> bool:
        return os.path.isdir(self._space_dir(space_name))

    def _remove_space_dir(self, space_name: str) -> None:
        path = self._space_dir(space_name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    def _space_reachable(self, space_name: str) -> bool:
        """table_names 可能漏登记磁盘上仍可打开的表（幽灵表），需目录/open 兜底。"""
        if space_name in self._table_names():
            return True
        if not self._space_dir_exists(space_name):
            return False
        try:
            self._open_table(space_name)
            return True
        except Exception as e:
            logging.warning(
                "LanceDB space %s 目录存在但无法打开，视为不存在: %s",
                space_name,
                e,
            )
            return False

    def _drop_space_sync(self, space_name: str) -> None:
        """从目录摘除表：优先 drop_table，并清理残留 .lance 目录。"""
        try:
            if space_name in self._table_names():
                self.db.drop_table(space_name)
        except Exception as e:
            logging.warning("LanceDB drop_table %s failed: %s", space_name, e)
        self._remove_space_dir(space_name)
        self._space_dims.pop(space_name, None)

    def _ensure_space_table(self, space_name: str, vector_size: int) -> bool:
        schema = _build_table_schema(vector_size)
        if self._space_reachable(space_name):
            table = self._open_table(space_name)
            if _table_schema_compatible(table.schema, vector_size):
                return True
            logging.warning("LanceDB table %s schema 过时，将重建", space_name)
            self._drop_space_sync(space_name)
        self.db.create_table(
            space_name,
            schema=schema,
            mode="create",
            exist_ok=True,
        )
        logging.info("Created LanceDB table: %s", space_name)
        return True

    def get_db_type(self) -> str:
        return "lancedb"

    async def close(self) -> None:
        return None

    async def health_check(self) -> dict[str, Any]:
        tables = await asyncio.to_thread(self._table_names)
        return {"status": "ok", "uri": self.uri, "tables": len(tables)}

    def _table_names(self) -> list[str]:
        # LanceDB 0.21 table_names 默认 limit=10，超出部分会被截断，导致
        # `name in table_names()` 假阴性（见 lancedb#2727）。必须翻页取全量。
        names: list[str] = []
        page_token: Optional[str] = None
        page_size = int(self.TABLE_NAMES_PAGE_SIZE)
        while True:
            batch = list(self.db.table_names(page_token=page_token, limit=page_size))
            if not batch:
                break
            names.extend(batch)
            if len(batch) < page_size:
                break
            page_token = batch[-1]
        return names

    def _open_table(self, space_name: str):
        return self.db.open_table(space_name)

    async def create_space(self, space_name: str, vector_size: int, **kwargs) -> bool:
        self._space_dims[space_name] = vector_size
        lock = await self._get_space_lock(space_name)
        async with lock:
            try:
                return await asyncio.to_thread(self._ensure_space_table, space_name, vector_size)
            except Exception as e:
                if "already exists" in str(e).lower():
                    return True
                logging.error("Failed to create LanceDB table %s: %s", space_name, e)
                return False

    async def delete_space(self, space_name: str, **kwargs) -> bool:
        try:
            await asyncio.to_thread(self._drop_space_sync, space_name)
            return True
        except Exception as e:
            logging.error("Failed to delete LanceDB table %s: %s", space_name, e)
            return False

    async def space_exists(self, space_name: str, **kwargs) -> bool:
        return await asyncio.to_thread(self._space_reachable, space_name)

    def _prepare_record(self, record: dict[str, Any], vector_size: int) -> dict[str, Any]:
        row = dict(record)
        vector_field = _vector_field_name(vector_size)
        vector = row.get(vector_field)
        if vector is not None:
            row[vector_field] = [float(v) for v in vector]
        for key, value in list(row.items()):
            if value is None:
                continue
            if key == vector_field:
                continue
            if isinstance(value, (str, int, float, bool)):
                continue
            row[key] = str(value)
        return row

    async def insert_records(self, space_name: str, records: list[dict[str, Any]], **kwargs) -> list[str]:
        if not records:
            return []
        vector_size = self._space_dims.get(space_name)
        if vector_size is None:
            for key in records[0]:
                match = re.fullmatch(r"q_(\d+)_vec", key)
                if match:
                    vector_size = int(match.group(1))
                    self._space_dims[space_name] = vector_size
                    break
        if vector_size is None:
            logging.error("Cannot infer vector size for LanceDB table %s", space_name)
            return [record.get("id", "unknown") for record in records]

        prepared = [self._prepare_record(record, vector_size) for record in records]
        lock = await self._get_space_lock(space_name)
        async with lock:
            try:
                await asyncio.to_thread(self._ensure_space_table, space_name, vector_size)
                table = self._open_table(space_name)
                await asyncio.to_thread(table.add, prepared)
                return []
            except Exception as e:
                logging.error("Failed to insert records into LanceDB table %s: %s", space_name, e)
                return [str(record.get("id", "unknown")) for record in records]

    async def update_records(
        self,
        space_name: str,
        condition: dict[str, Any],
        new_value: dict[str, Any],
        fields_to_remove: list[str] = None,
        **kwargs,
    ) -> bool:
        logging.warning("LanceDB update_records is not implemented for %s", space_name)
        return False

    async def delete_records(self, space_name: str, condition: dict[str, Any], **kwargs) -> int:
        if not self._space_reachable(space_name):
            return 0
        where = _build_where(condition)
        if not where:
            return 0
        lock = await self._get_space_lock(space_name)
        async with lock:
            try:
                if not self._space_reachable(space_name):
                    return 0
                table = self._open_table(space_name)
                before = await asyncio.to_thread(table.count_rows)
                await asyncio.to_thread(table.delete, where)
                after = await asyncio.to_thread(table.count_rows)
                return max(before - after, 0)
            except Exception as e:
                logging.warning("LanceDB delete_records failed on %s: %s", space_name, e)
                return 0

    async def get_record(self, space_names: list[str], record_id: str, **kwargs) -> Optional[dict[str, Any]]:
        if not space_names or len(space_names) != 1:
            return None
        space_name = space_names[0]
        if not self._space_reachable(space_name):
            return None
        try:
            table = self._open_table(space_name)
            where = f"id = '{_escape_sql_string(record_id)}'"
            rows = await asyncio.to_thread(
                lambda: table.search().where(where).limit(1).to_list()
            )
            if not rows:
                return None
            row = dict(rows[0])
            row["id"] = record_id
            row.pop("_distance", None)
            return row
        except Exception as e:
            logging.error("LanceDB get_record failed: %s", e)
            return None

    async def list_records(
        self,
        space_name: str,
        *,
        condition: Optional[dict[str, Any]] = None,
        select_fields: Optional[list[str]] = None,
        limit: int = 500,
        **kwargs,
    ) -> list[dict[str, Any]]:
        if not self._space_reachable(space_name):
            return []

        def _run() -> list[dict[str, Any]]:
            table = self._open_table(space_name)
            try:
                frame = table.to_pandas()
            except Exception:
                # 兼容旧版：无向量条件扫表
                where = _build_where(condition)
                builder = table.search()
                if where:
                    builder = builder.where(where, prefilter=True)
                fields = list(select_fields or [])
                if "id" not in fields:
                    fields = fields + ["id"] if fields else ["id"]
                take = limit if limit and limit > 0 else max(int(table.count_rows()), 1)
                rows = builder.select(fields).limit(take).to_list()
                return [dict(r) for r in rows]

            if condition:
                for key, value in condition.items():
                    if key not in frame.columns or value is None or value == "":
                        continue
                    frame = frame[frame[key] == value]
            if select_fields:
                cols = [c for c in select_fields if c in frame.columns]
                if "id" in frame.columns and "id" not in cols:
                    cols = ["id"] + cols
                frame = frame[cols] if cols else frame
            if limit and limit > 0:
                frame = frame.head(limit)
            records: list[dict[str, Any]] = []
            for _, row in frame.iterrows():
                item = row.to_dict()
                for k, v in list(item.items()):
                    if hasattr(v, "tolist"):
                        item[k] = v.tolist()
                    elif hasattr(v, "item") and not isinstance(v, (bytes, str)):
                        try:
                            item[k] = v.item()
                        except Exception:
                            item[k] = v
                records.append(item)
            return records

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            logging.error("LanceDB list_records failed on %s: %s", space_name, e)
            raise

    async def search(self, space_names: list[str], request: SearchRequest, **kwargs) -> dict[str, Any]:
        if not space_names:
            return {"hits": {"hits": [], "total": {"value": 0}}}
        space_name = space_names[0]
        if not self._space_reachable(space_name):
            return {"hits": {"hits": [], "total": {"value": 0}}}

        dense_expr: Optional[MatchDenseExpr] = None
        if request.match_exprs:
            for expr in request.match_exprs:
                if isinstance(expr, MatchDenseExpr):
                    dense_expr = expr
                    break
                if isinstance(expr, MatchTextExpr):
                    logging.warning("LanceDB does not support MatchTextExpr, ignoring text match")

        table = self._open_table(space_name)
        where = _build_where(request.condition)
        limit = request.limit if request.limit > 0 else 10

        def _run_search() -> list[dict[str, Any]]:
            if dense_expr is None:
                builder = table.search()
            else:
                search_kwargs = {}
                if dense_expr.vector_column_name:
                    search_kwargs["vector_column_name"] = dense_expr.vector_column_name
                builder = (
                    table.search(list(dense_expr.embedding_data), **search_kwargs)
                    .metric("cosine" if dense_expr.distance_type == "cosine" else "l2")
                )
            if where:
                builder = builder.where(where, prefilter=True)
            if request.select_fields:
                builder = builder.select(request.select_fields + ["id"])
            else:
                builder = builder.select(["id"])
            return builder.limit(limit).to_list()

        try:
            rows = await asyncio.to_thread(_run_search)
        except Exception as e:
            logging.error("LanceDB search failed on %s: %s", space_name, e)
            raise

        hits = []
        for row in rows:
            source = dict(row)
            record_id = str(source.pop("id", ""))
            distance = source.pop("_distance", None)
            score = 1.0 - float(distance) if distance is not None else 0.0
            hits.append({"_id": record_id, "_score": score, "_source": source})
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}}

    def get_total(self, result) -> int:
        try:
            if "hits" in result and "total" in result["hits"]:
                total_count = result["hits"]["total"]
                if isinstance(total_count, dict):
                    return int(total_count.get("value", 0))
                return int(total_count)
            return 0
        except Exception as e:
            logging.error("get_total error: %s", e)
            return 0

    def get_chunk_ids(self, result) -> list[str]:
        try:
            return [hit["_id"] for hit in result.get("hits", {}).get("hits", []) if "_id" in hit]
        except Exception as e:
            logging.error("get_chunk_ids error: %s", e)
            return []

    def get_source(self, result) -> list[dict[str, Any]]:
        sources = []
        for hit in result.get("hits", {}).get("hits", []):
            source = dict(hit.get("_source", {}))
            source["id"] = hit.get("_id")
            source["_score"] = hit.get("_score")
            sources.append(source)
        return sources

    def get_fields(self, result, fields: list[str]) -> dict[str, dict]:
        field_data: dict[str, dict] = {}
        for source in self.get_source(result):
            data = {name: source.get(name) for name in fields if source.get(name) is not None}
            if data:
                field_data[str(source.get("id"))] = data
        return field_data

    def get_highlight(self, result, keywords: list[str], field_name: str) -> dict[str, Any]:
        return {}

    def get_aggregation(self, result, field_name: str) -> dict[str, Any]:
        return []

    async def sql(self, sql: str, fetch_size: int, format: str):
        raise NotImplementedError("LanceDB SQL interface is not implemented")
