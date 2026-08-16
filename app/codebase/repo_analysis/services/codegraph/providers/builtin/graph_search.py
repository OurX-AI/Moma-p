import os
from typing import Dict, List
from app.config.settings import settings
from app.utils.common import normalize_path
from ...model import QueryResponse
from .neo4j_service import Neo4jService


class CodeGraphSearch:
    def __init__(self):
        """初始化查询工具"""
        self.db_client = None
        if settings.code_graph_enabled:
            self.db_client = Neo4jService(
                settings.neo4j_uri,
                settings.neo4j_user,
                settings.neo4j_password,
            )

    @staticmethod
    def _disabled_response() -> QueryResponse:
        return QueryResponse(
            result=False,
            content={},
            message="CodeGraph 未启用，请在 env 中设置 CODE_GRAPH_ENABLED=true",
        )
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):  
        if self.db_client:
            self.db_client.close()
            self.db_client = None
    
    async def query_dependents_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        """查询依赖本文件的其他文件列表。"""
        if not settings.code_graph_enabled:
            return self._disabled_response()
        try:
            normalized_file_path = normalize_path(os.path.normpath(file_path))
            dependents: List[str] = self.db_client.query_dependents_of_file(repo_id, normalized_file_path)
            return QueryResponse(
                result=True,
                content={"dependents": dependents},
            )
        except Exception as e:
            return QueryResponse(
                result=False,
                content={},
                message=f"Failed to query dependents: {str(e)}",
            )

    async def query_dependented_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        """查询本文件被依赖（即本文件依赖的其他文件列表）。"""
        if not settings.code_graph_enabled:
            return self._disabled_response()
        try:
            normalized_file_path = normalize_path(os.path.normpath(file_path))
            dependented: List[str] = self.db_client.query_dependented_of_file(repo_id, normalized_file_path)
            return QueryResponse(
                result=True,
                content={"dependented": dependented},
            )
        except Exception as e:
            return QueryResponse(
                result=False,
                content={},
                message=f"Failed to query dependented: {str(e)}",
            )

    async def query_file_summary(self, repo_id: str, file_paths: List[str]) -> QueryResponse:
        """查询文件内容概述（包含类/方法/顶层函数清单）。"""
        if not settings.code_graph_enabled:
            return self._disabled_response()
        try:
            normalized_paths = [normalize_path(os.path.normpath(p)) for p in file_paths]
            records = self.db_client.query_file_summary(repo_id, normalized_paths)

            files_summary: Dict[str, object] = {}
            for record in records:
                files_summary[record["path"]] = {
                    "name": record["name"],
                    "language": record["language"],
                    "classes": [
                        {
                            "name": cls["name"],
                            "full_name": cls["full_name"],
                            "methods": [m for m in cls["methods"] if m.get("name") is not None],
                        }
                        for cls in (record.get("classes") or [])
                        if cls.get("name") is not None
                    ],
                    "functions": [
                        func for func in (record.get("functions") or [])
                        if func.get("name") is not None
                    ],
                }

            return QueryResponse(
                result=True,
                content={"files": files_summary},
            )
        except Exception as e:
            return QueryResponse(
                result=False,
                content={},
                message=f"Failed to query file summary: {str(e)}",
            )