from typing import Dict
from sqlalchemy import select
from app.infrastructure.database import get_db_session
from app.infrastructure.llms import embedding_factory
from app.infrastructure.vector_store import VECTOR_STORE_CONN
from ..constants import api_summary_space_name
from .api_vector import ApiVectorSearchService
from ...repo_mgmt.models.git_repo_mgmt import GitRepository, RepoKind


class LibSearchService:
    """Lib 公开接口检索编排。"""

    @staticmethod
    async def search_apis(
        repo_id: str,
        query: str,
        top_k: int = 10,
    ) -> Dict[str, object]:
        q = (query or "").strip()
        if not q:
            raise ValueError("query 不能为空")

        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            kind = getattr(repo, "kind", None) or RepoKind.CODE
            if kind != RepoKind.LIB:
                raise ValueError(f"search api 仅支持 kind=lib，当前 kind={kind}")

        await LibSearchService._assert_api_index_ready(repo_id)

        docs = await ApiVectorSearchService.search_api_summaries(repo_id, q, top_k)
        return {
            "repo_id": repo_id,
            "query": q,
            "total": len(docs),
            "items": [
                {
                    "file_path": doc.get("file_path"),
                    "api_kind": doc.get("api_kind"),
                    "api_name": doc.get("api_name"),
                    "signature": doc.get("signature"),
                    "start_line": doc.get("start_line"),
                    "end_line": doc.get("end_line"),
                    "summary": doc.get("summary"),
                }
                for doc in docs
            ],
        }

    @staticmethod
    async def _assert_api_index_ready(repo_id: str) -> None:
        """未分析或尚无 API 向量空间时明确报错，避免空结果冒充成功。"""
        model = embedding_factory.create_model()
        if not model:
            raise ValueError("embedding 模型不可用，无法检索 Lib API")
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            raise ValueError("embedding 模型不可用，无法检索 Lib API")
        dim = len(vectors[0])
        space = api_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            raise ValueError("该 Lib 尚未完成分析或无 API 向量数据，请先执行 analyze")
