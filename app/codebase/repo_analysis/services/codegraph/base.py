from abc import ABC, abstractmethod
from typing import List, Optional
from .model import QueryResponse


class CodeGraphGeneratorBase(ABC):
    """图谱生成器统一契约。"""

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_repo_graph(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_file_graph(self, rel_file_path: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def generate_graph(self, clean_stale: bool = False):
        raise NotImplementedError

    async def update_files(self, file_paths: List[str]):
        return None

    async def refresh_graph(self) -> None:
        """增量刷新图谱。仅 builtin provider 实现，CLI provider 使用自身同步机制。"""
        return None


class CodeGraphSearchBase(ABC):
    """图谱检索统一契约。"""

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def query_dependents_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        raise NotImplementedError

    @abstractmethod
    async def query_dependented_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        raise NotImplementedError

    @abstractmethod
    async def query_file_summary(self, repo_id: str, file_paths: List[str]) -> QueryResponse:
        raise NotImplementedError

    async def query_callers_of_symbol(
        self,
        repo_id: str,
        symbol: str,
        limit: int = 20,
    ) -> QueryResponse:
        return QueryResponse(
            result=False,
            content={},
            message="当前 CodeGraph provider 未实现 query_callers_of_symbol",
        )

    async def query_callees_of_symbol(
        self,
        repo_id: str,
        symbol: str,
        limit: int = 20,
    ) -> QueryResponse:
        return QueryResponse(
            result=False,
            content={},
            message="当前 CodeGraph provider 未实现 query_callees_of_symbol",
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class CodeGraphProvider(ABC):
    """CodeGraph 提供方（自研 / 开源）。"""

    name: str = "base"

    @abstractmethod
    def ensure_ready(self) -> None:
        """启动时检查依赖是否可用，必要时安装。"""
        raise NotImplementedError

    @abstractmethod
    def create_generator(
        self,
        repo_id: str,
        repo_name: str,
        repo_local_path: str,
    ) -> CodeGraphGeneratorBase:
        raise NotImplementedError

    @abstractmethod
    def create_search(self) -> CodeGraphSearchBase:
        raise NotImplementedError
