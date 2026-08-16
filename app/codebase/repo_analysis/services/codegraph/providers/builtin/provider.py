from typing import List
from ...base import (
    CodeGraphGeneratorBase,
    CodeGraphProvider,
    CodeGraphSearchBase,
)
from ...model import QueryResponse


class _BuiltinGenerator(CodeGraphGeneratorBase):
    def __init__(self, inner):
        self._inner = inner

    def close(self) -> None:
        self._inner.close()

    async def delete_repo_graph(self) -> None:
        await self._inner.delete_repo_graph()

    async def delete_file_graph(self, rel_file_path: str) -> None:
        await self._inner.delete_file_graph(rel_file_path)

    async def generate_graph(self, clean_stale: bool = False):
        return await self._inner.generate_graph(clean_stale=clean_stale)

    async def refresh_graph(self) -> None:
        await self._inner.refresh_graph()


class _BuiltinSearch(CodeGraphSearchBase):
    def __init__(self, inner):
        self._inner = inner

    def close(self) -> None:
        if getattr(self._inner, "db_client", None):
            self._inner.db_client.close()
            self._inner.db_client = None

    async def query_dependents_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        return await self._inner.query_dependents_of_file(repo_id, file_path)

    async def query_dependented_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        return await self._inner.query_dependented_of_file(repo_id, file_path)

    async def query_file_summary(self, repo_id: str, file_paths: List[str]) -> QueryResponse:
        return await self._inner.query_file_summary(repo_id, file_paths)


class BuiltinCodeGraphProvider(CodeGraphProvider):
    """自研 CodeGraph（AST + Neo4j）。"""

    name = "builtin"

    def ensure_ready(self) -> None:
        from app.config.settings import settings
        if not settings.code_graph_enabled:
            return
        if not (settings.neo4j_uri or "").strip():
            raise RuntimeError("CODE_GRAPH_PROVIDER=builtin 时需配置 NEO4J_URI")
        try:
            import neo4j  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("builtin CodeGraph 需要安装 neo4j 驱动包") from exc

    def create_generator(
        self,
        repo_id: str,
        repo_name: str,
        repo_local_path: str,
    ) -> CodeGraphGeneratorBase:
        from .graph_creator import CodeGraphGenerator
        return _BuiltinGenerator(CodeGraphGenerator(repo_id, repo_name, repo_local_path))

    def create_search(self) -> CodeGraphSearchBase:
        from .graph_search import CodeGraphSearch
        return _BuiltinSearch(CodeGraphSearch())
