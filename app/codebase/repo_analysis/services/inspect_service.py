from __future__ import annotations
import json
import os
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from app.infrastructure.database import get_db_session
from app.infrastructure.llms import embedding_factory
from app.infrastructure.vector_store import VECTOR_STORE_CONN
from ...lib_analysis.constants import LibAnalysisType, api_summary_space_name
from ..constants import line_chunk_space_name
from ..models.analysis_status import RepoAnalysisType, RepoFileAnalysisState
from .codegraph.gateway import CodeGraphGateway
from ...repo_mgmt.models.git_repo_mgmt import GitRepository, RepoKind
from app.utils.common import normalize_path


class InspectService:
    """只读验收：切片 / Graph / Lib API 导出。"""

    DEFAULT_LIMIT = 500
    CONTENT_PREVIEW_LEN = 200

    @staticmethod
    def normalize_target(target: Optional[str]) -> Optional[str]:
        if target is None:
            return None
        t = normalize_path(str(target).strip()).strip("/")
        return t or None

    @staticmethod
    def match_path(file_path: str, target: Optional[str]) -> bool:
        if not target:
            return True
        fp = normalize_path(file_path or "").strip("/")
        tg = target.strip("/")
        if not fp:
            return False
        if fp == tg:
            return True
        return fp.startswith(tg + "/")

    @staticmethod
    def apply_limit(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        if limit and limit > 0:
            return items[:limit]
        return items

    @staticmethod
    async def _embedding_dim() -> int:
        model = embedding_factory.create_model()
        if not model:
            raise ValueError("embedding 模型不可用")
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            raise ValueError("embedding 模型不可用")
        return len(vectors[0])

    @staticmethod
    async def _require_repo(repo_id: str, expect_kind: str) -> GitRepository:
        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            kind = getattr(repo, "kind", None) or RepoKind.CODE
            if kind != expect_kind:
                raise ValueError(f"该 inspect 仅支持 kind={expect_kind}，当前 kind={kind}")
            return repo

    @classmethod
    async def inspect_chunks(
        cls,
        repo_id: str,
        *,
        target: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
        full_content: bool = False,
    ) -> Dict[str, Any]:
        await cls._require_repo(repo_id, RepoKind.CODE)
        target_n = cls.normalize_target(target)
        dim = await cls._embedding_dim()
        space = line_chunk_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            raise ValueError("该仓库尚无切片向量数据，请先执行 analyze")

        # 有 target 时先多取再过滤；无 target 时直接按 limit 取；limit=0 表示尽量全量
        fetch_limit = 0 if not limit or limit <= 0 else (max(limit * 20, 2000) if target_n else limit)
        rows = await VECTOR_STORE_CONN.list_records(
            space,
            condition={
                "repo_id": repo_id,
                "analysis_type": RepoAnalysisType.LINE_CHUNK_VECTOR.value,
            },
            select_fields=[
                "repo_id",
                "file_path",
                "analysis_type",
                "start_line",
                "end_line",
                "chunk_index",
                "content",
            ],
            limit=fetch_limit if fetch_limit > 0 else 0,
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            fp = str(row.get("file_path") or "")
            if not cls.match_path(fp, target_n):
                continue
            content = str(row.get("content") or "")
            item: Dict[str, Any] = {
                "file_path": fp,
                "start_line": row.get("start_line"),
                "end_line": row.get("end_line"),
                "chunk_index": row.get("chunk_index"),
            }
            if full_content:
                item["content"] = content
            else:
                item["content_preview"] = content[: cls.CONTENT_PREVIEW_LEN]
            items.append(item)
        items.sort(key=lambda x: (str(x.get("file_path") or ""), int(x.get("start_line") or 0)))
        truncated = cls.apply_limit(items, limit)
        return {
            "repo_id": repo_id,
            "target": target_n,
            "total": len(truncated),
            "matched_before_limit": len(items),
            "limit": limit,
            "items": truncated,
        }

    @classmethod
    async def inspect_graph(
        cls,
        repo_id: str,
        *,
        target: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        await cls._require_repo(repo_id, RepoKind.CODE)
        target_n = cls.normalize_target(target)

        async with get_db_session() as db:
            paths = (
                await db.scalars(
                    select(RepoFileAnalysisState.file_path).where(RepoFileAnalysisState.repo_id == repo_id)
                )
            ).all()
        file_paths = sorted(
            {normalize_path(str(p)).strip("/") for p in paths if p and cls.match_path(str(p), target_n)}
        )
        if not file_paths:
            # 无文件状态时，若指定了单文件 target，仍尝试直接查 graph
            if target_n and "." in os.path.basename(target_n):
                file_paths = [target_n]
            else:
                raise ValueError("未找到可检查的文件状态，请先执行 analyze")

        file_paths = cls.apply_limit(file_paths, limit)
        files_out: List[Dict[str, Any]] = []
        with CodeGraphGateway.create_search() as q:
            for fp in file_paths:
                deps_res = await q.query_dependented_of_file(repo_id, fp)
                dents_res = await q.query_dependents_of_file(repo_id, fp)
                dependencies = []
                dependents = []
                if deps_res.result and isinstance(deps_res.content, dict):
                    dependencies = list(deps_res.content.get("dependented") or [])
                if dents_res.result and isinstance(dents_res.content, dict):
                    dependents = list(dents_res.content.get("dependents") or [])
                files_out.append(
                    {
                        "file_path": fp,
                        "dependencies": dependencies,
                        "dependents": dependents,
                        "dependencies_ok": bool(deps_res.result),
                        "dependents_ok": bool(dents_res.result),
                        "dependencies_error": None if deps_res.result else deps_res.message,
                        "dependents_error": None if dents_res.result else dents_res.message,
                    }
                )
        return {
            "repo_id": repo_id,
            "target": target_n,
            "total": len(files_out),
            "limit": limit,
            "files": files_out,
        }

    @classmethod
    async def inspect_apis(
        cls,
        repo_id: str,
        *,
        file_path: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        await cls._require_repo(repo_id, RepoKind.LIB)
        file_n = cls.normalize_target(file_path)
        dim = await cls._embedding_dim()
        space = api_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            raise ValueError("该 Lib 尚无 API 向量数据，请先执行 analyze")

        fetch_limit = 0 if not limit or limit <= 0 else (max(limit * 20, 2000) if file_n else limit)
        rows = await VECTOR_STORE_CONN.list_records(
            space,
            condition={
                "repo_id": repo_id,
                "analysis_type": LibAnalysisType.API_SUMMARY_VECTOR,
            },
            select_fields=[
                "repo_id",
                "file_path",
                "analysis_type",
                "symbol_kind",
                "symbol_name",
                "content",
                "summary",
                "start_line",
                "end_line",
            ],
            limit=fetch_limit if fetch_limit > 0 else 0,
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            fp = str(row.get("file_path") or "")
            if not cls.match_path(fp, file_n):
                continue
            items.append(
                {
                    "file_path": fp,
                    "api_kind": row.get("symbol_kind"),
                    "api_name": row.get("symbol_name"),
                    "signature": row.get("content"),
                    "summary": row.get("summary"),
                    "start_line": row.get("start_line"),
                    "end_line": row.get("end_line"),
                }
            )
        items.sort(key=lambda x: (str(x.get("file_path") or ""), str(x.get("api_name") or "")))
        truncated = cls.apply_limit(items, limit)
        return {
            "repo_id": repo_id,
            "file": file_n,
            "total": len(truncated),
            "matched_before_limit": len(items),
            "limit": limit,
            "items": truncated,
        }

    @staticmethod
    def write_export(data: Dict[str, Any], export_path: str) -> str:
        abs_path = os.path.abspath(export_path)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        return abs_path
