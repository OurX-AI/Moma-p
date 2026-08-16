import asyncio
import hashlib
import logging
from typing import Dict, List
from app.config.settings import settings
from app.infrastructure.vector_store import VECTOR_STORE_CONN
from ...constants import LibAnalysisType, api_summary_space_name
from ...schemes.public_api import PublicApi
from ..api_summary import ApiSummaryService
from ....repo_analysis.services.codevector.code_vector import CodeVectorService
from app.utils.common import normalize_path


class ApiVectorService:
    """Lib 公开接口摘要向量化与落库。

    写入字段复用 LanceDB 通用 schema：
    - symbol_kind / symbol_name / summary / content(签名)
    """

    @staticmethod
    async def vectorize_and_store_apis(
        repo_id: str,
        rel_file_path: str,
        apis: List[PublicApi],
    ) -> None:
        rel_file_path = normalize_path(rel_file_path)
        if not apis:
            await ApiVectorService.delete_file_vector_records(repo_id, rel_file_path)
            return

        sem = asyncio.Semaphore(max(1, settings.code_analysis_symbol_summary_llm_concurrency))

        async def one(api: PublicApi) -> str:
            async with sem:
                return await ApiSummaryService.summarize(api)

        summaries = await asyncio.gather(*[one(api) for api in apis])
        kept_apis = []
        texts: List[str] = []
        for i, s in enumerate(summaries):
            t = (s or "").strip() or ApiSummaryService.fallback_summary(apis[i])
            t = (t or "").strip()
            if not t:
                logging.warning(
                    "跳过空 API 摘要 embedding file=%s api=%s",
                    rel_file_path,
                    apis[i].display_name(),
                )
                continue
            kept_apis.append(apis[i])
            texts.append(t)
        if not texts:
            return
        apis = kept_apis
        vectors = await CodeVectorService._embed_texts(texts)
        if not vectors:
            raise RuntimeError("api summary 向量化失败")

        dim = len(vectors[0])
        vector_field = f"q_{dim}_vec"
        space_name = api_summary_space_name(repo_id, dim)
        await VECTOR_STORE_CONN.create_space(space_name, dim)
        await VECTOR_STORE_CONN.delete_records(
            space_name,
            {
                "repo_id": repo_id,
                "file_path": rel_file_path,
                "analysis_type": LibAnalysisType.API_SUMMARY_VECTOR,
            },
        )

        records: List[Dict[str, object]] = []
        for idx, api in enumerate(apis):
            display = api.display_name()
            stable_id = ApiVectorService._build_stable_id(
                repo_id=repo_id,
                file_path=rel_file_path,
                api_kind=api.kind,
                api_name=display,
                start_line=api.start_line,
                end_line=api.end_line,
                extra=str(idx),
            )
            records.append(
                {
                    "id": stable_id,
                    "repo_id": repo_id,
                    "file_path": rel_file_path,
                    "analysis_type": LibAnalysisType.API_SUMMARY_VECTOR,
                    "symbol_kind": api.kind,
                    "symbol_name": display,
                    "content": api.signature,
                    "language": api.language,
                    "start_line": api.start_line,
                    "end_line": api.end_line,
                    "summary": texts[idx],
                    vector_field: vectors[idx],
                }
            )
        # language 不在通用 schema 中：写入前去掉，避免 LanceDB 报 Field not found
        for row in records:
            row.pop("language", None)

        failed_ids = await VECTOR_STORE_CONN.insert_records(space_name, records)
        if failed_ids:
            raise RuntimeError(f"api summary 写入向量失败: {len(failed_ids)}")

    @staticmethod
    def _build_stable_id(
        repo_id: str,
        file_path: str,
        api_kind: str,
        api_name: str,
        start_line: int,
        end_line: int,
        extra: str = "",
    ) -> str:
        raw = f"{repo_id}|{file_path}|{LibAnalysisType.API_SUMMARY_VECTOR}|{api_kind}|{api_name}|{start_line}|{end_line}|{extra}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    async def delete_file_vector_records(repo_id: str, rel_file_path: str) -> int:
        rel_file_path = normalize_path(rel_file_path)
        from app.infrastructure.llms import embedding_factory

        model = embedding_factory.create_model()
        if not model:
            return 0
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return 0
        dim = len(vectors[0])
        space_name = api_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space_name):
            return 0
        return int(
            await VECTOR_STORE_CONN.delete_records(
                space_name,
                {"repo_id": repo_id, "file_path": rel_file_path},
            )
        )

    @staticmethod
    async def delete_repo_vector_records(repo_id: str) -> int:
        from app.infrastructure.llms import embedding_factory

        model = embedding_factory.create_model()
        if not model:
            return 0
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return 0
        dim = len(vectors[0])
        space_name = api_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space_name):
            return 0
        deleted = int(await VECTOR_STORE_CONN.delete_records(space_name, {"repo_id": repo_id}))
        logging.info("已删除 Lib API 向量 repo_id=%s count=%s", repo_id, deleted)
        return deleted
