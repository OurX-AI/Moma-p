from typing import Dict, List
from app.infrastructure.vector_store import MatchDenseExpr, SearchRequest, VECTOR_STORE_CONN
from ...constants import LibAnalysisType, api_summary_space_name
from ....repo_analysis.services.codevector.code_vector import CodeVectorService


class ApiVectorSearchService:
    """Lib 公开接口摘要向量检索。"""

    @staticmethod
    async def search_api_summaries(
        repo_id: str,
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, object]]:
        text = (query or "").strip()
        if not text:
            return []
        rows = await CodeVectorService._embed_texts([text])
        if not rows:
            return []
        dim = len(rows[0])
        space = api_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return []
        request = SearchRequest(
            select_fields=[
                "repo_id",
                "file_path",
                "analysis_type",
                "symbol_kind",
                "symbol_name",
                "content",
                "start_line",
                "end_line",
                "summary",
            ],
            condition={"repo_id": repo_id, "analysis_type": LibAnalysisType.API_SUMMARY_VECTOR},
            match_exprs=[
                MatchDenseExpr(
                    vector_column_name=f"q_{dim}_vec",
                    embedding_data=rows[0],
                    embedding_data_type="float",
                    distance_type="cosine",
                    topn=top_k,
                )
            ],
            limit=top_k,
        )
        result = await VECTOR_STORE_CONN.search([space], request)
        docs = VECTOR_STORE_CONN.get_source(result) if result else []
        mapped: List[Dict[str, object]] = []
        for doc in docs:
            mapped.append(
                {
                    "repo_id": doc.get("repo_id"),
                    "file_path": doc.get("file_path"),
                    "analysis_type": doc.get("analysis_type"),
                    "api_kind": doc.get("symbol_kind"),
                    "api_name": doc.get("symbol_name"),
                    "signature": doc.get("content"),
                    "start_line": doc.get("start_line"),
                    "end_line": doc.get("end_line"),
                    "summary": doc.get("summary"),
                    "_score": doc.get("_score"),
                }
            )
        return mapped
