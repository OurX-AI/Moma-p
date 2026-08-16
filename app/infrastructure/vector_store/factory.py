from typing import Optional
import logging
import os
from app.config import settings
from app.infrastructure.vector_store.base import VectorStoreConnection
from app.infrastructure.vector_store.es_conn import ESConnection
from app.infrastructure.vector_store.lance_conn import LanceDBConnection
from app.infrastructure.vector_store.opensearch_conn import OSConnection


class VectorStoreFactory:
    """向量存储工厂类"""

    def __init__(self):
        self._connection: Optional[VectorStoreConnection] = None
        self._connection_type: Optional[str] = None
        self._mapping_name: Optional[str] = None

    def create_connection(self, db_type: str = None, mapping_name: str = None) -> VectorStoreConnection:
        actual_db_type = db_type or settings.vector_store_engine
        actual_mapping_name = mapping_name or settings.vector_store_mapping
        engine = actual_db_type.lower()
        try:
            if engine == "lancedb":
                connection = LanceDBConnection(uri=settings.lancedb_uri)
            elif engine == "elasticsearch":
                connection = ESConnection(
                    hosts=settings.es_hosts,
                    username=settings.es_username,
                    password=settings.es_password,
                    mapping_name=actual_mapping_name,
                    verify_certs=settings.es_verify_certs,
                )
            elif engine == "opensearch":
                connection = OSConnection(
                    hosts=settings.os_hosts,
                    username=settings.os_username,
                    password=settings.os_password,
                    mapping_name=actual_mapping_name,
                )
            else:
                raise ValueError(
                    f"不支持的数据库类型: {engine}，可选: lancedb, elasticsearch, opensearch"
                )
            self._connection = connection
            self._connection_type = actual_db_type
            self._mapping_name = actual_mapping_name
            logging.info("向量存储连接创建成功: %s", actual_db_type)
            return connection
        except Exception as e:
            logging.error("创建向量存储连接失败: %s", e)
            raise


_vector_store_factory = VectorStoreFactory()
_vector_store_conn: Optional[VectorStoreConnection] = None


def get_vector_store_conn() -> VectorStoreConnection:
    global _vector_store_conn
    if _vector_store_conn is None:
        _vector_store_conn = _vector_store_factory.create_connection()
    return _vector_store_conn


class _LazyVectorStoreConn:
    """首次访问时才加载向量库实现。"""

    def __getattr__(self, name: str):
        return getattr(get_vector_store_conn(), name)


VECTOR_STORE_CONN = _LazyVectorStoreConn()
