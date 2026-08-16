from typing import Dict, List
from ...constants import line_chunk_space_name, symbol_summary_space_name
from ...models.analysis_status import RepoAnalysisType as AnalysisType
from .code_vector import CodeVectorService
from app.infrastructure.vector_store import MatchDenseExpr, SearchRequest, VECTOR_STORE_CONN


class CodeVectorSearchService:
    """代码仓向量索引检索：行块向量、符号摘要向量（与 CodeVectorService 写入空间一致）。"""

    @staticmethod
    async def search_code_chunk_vectors(
        repo_id: str,
        contexts: List[str],
        top_k: int = 10,
    ) -> List[Dict[str, object]]:
        texts = [str(t).strip() for t in (contexts or []) if t and str(t).strip()]
        if not texts:
            return []

        # 向量化请求内容     
        rows = await CodeVectorService._embed_texts(texts)
        if not rows:
            return []

        # 计算平均向量
        n = len(rows)
        dim = len(rows[0])
        query = [sum(rows[i][j] for i in range(n)) / n for j in range(dim)]

        # 检查向量空间是否存在
        space = line_chunk_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return []

        # 构建搜索请求
        request = SearchRequest(
            select_fields=["repo_id", "file_path", "analysis_type", "start_line", "end_line", "content"],
            condition={"repo_id": repo_id, "analysis_type": AnalysisType.LINE_CHUNK_VECTOR.value},
            match_exprs=[
                MatchDenseExpr(
                    vector_column_name=f"q_{dim}_vec",
                    embedding_data=query,
                    embedding_data_type="float",
                    distance_type="cosine",
                    topn=top_k,
                )
            ],
            limit=top_k,
        )

        # 执行搜索
        result = await VECTOR_STORE_CONN.search([space], request)
        return VECTOR_STORE_CONN.get_source(result) if result else []

    @staticmethod
    async def search_code_symbol_summary_vectors(
        repo_id: str,
        contexts: List[str],
        top_k: int = 10,
    ) -> List[Dict[str, object]]:
        texts = [str(t).strip() for t in contexts if t and str(t).strip()]
        if not texts:
            return []

        # 向量化请求内容
        rows = await CodeVectorService._embed_texts(texts)
        if not rows:
            return []

        # 计算平均向量
        n = len(rows)
        dim = len(rows[0])
        query = [sum(rows[i][j] for i in range(n)) / n for j in range(dim)]

        # 检查向量空间是否存在
        space = symbol_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return []

        # 构建搜索请求
        request = SearchRequest(
            select_fields=[
                "repo_id",
                "file_path",
                "analysis_type",
                "symbol_kind",
                "symbol_name",
                "start_line",
                "end_line",
                "summary",
            ],
            condition={"repo_id": repo_id, "analysis_type": AnalysisType.SYMBOL_SUMMARY_VECTOR.value},
            match_exprs=[
                MatchDenseExpr(
                    vector_column_name=f"q_{dim}_vec",
                    embedding_data=query,
                    embedding_data_type="float",
                    distance_type="cosine",
                    topn=top_k,
                )
            ],
            limit=top_k,
        )

        # 执行搜索
        result = await VECTOR_STORE_CONN.search([space], request)
        return VECTOR_STORE_CONN.get_source(result) if result else []
