from typing import Dict, List
from .dto import AnalyzeResult, IndexStatusResult, ReadyResult, ResolveResult
from .orchestrator import AutoAnalyzeOrchestrator
from ..repo_analysis.services.analysis_service import AnalysisService
from ..repo_analysis.services.codegraph.gateway import CodeGraphGateway
from ..repo_analysis.services.search_index_meta import SearchIndexMeta
from ..repo_analysis.services.search_service import SearchService
from ..repo_analysis.services.search_resolve.resolve_service import SearchResolveService


class CodebaseFacade:
    @staticmethod
    def _split_keywords(query: str) -> List[str]:
        raw = str(query or "").replace(",", " ").replace("\n", " ")
        return [token.strip() for token in raw.split(" ") if token.strip()]

    @staticmethod
    async def ensure_workspace_ready(workspace_path: str, user_id: str) -> ReadyResult:
        return await AutoAnalyzeOrchestrator.ensure_workspace_ready(workspace_path, user_id)

    @staticmethod
    async def ensure_workspace_registered(workspace_path: str, user_id: str) -> ReadyResult:
        return await AutoAnalyzeOrchestrator.ensure_workspace_registered(workspace_path, user_id)

    @staticmethod
    async def resolve_code(query: str, repo_id: str) -> ResolveResult:
        payload = await SearchResolveService.resolve(repo_id=repo_id, query=query, require_searchable=False)
        return ResolveResult(
            ok=True,
            repo_id=repo_id,
            query=query,
            message="查询完成",
            data=payload,
        )

    @staticmethod
    async def get_index_status(repo_id: str) -> IndexStatusResult:
        scan = await AnalysisService.get_scan_status(repo_id=repo_id)
        index_meta = await SearchIndexMeta.for_repo(repo_id=repo_id)
        status = str(scan.get("scan_status") or index_meta.get("scan_status") or "")
        age = index_meta.get("index_age_seconds")
        return IndexStatusResult(
            ok=True,
            repo_id=repo_id,
            status=status,
            index_age_seconds=age if isinstance(age, (int, float)) else None,
            message="索引状态已获取",
            data={"scan": scan, "index": index_meta},
        )

    @staticmethod
    async def can_query(repo_id: str) -> bool:
        """轻量判断 CodeBase 是否可查询（仅计数 needed，不拉全量 summary）。"""
        return await AnalysisService.can_query(repo_id)

    @staticmethod
    async def locate_symbol(repo_id: str, query: str, top_k: int = 10) -> Dict[str, object]:
        tokens = CodebaseFacade._split_keywords(query)
        if not tokens:
            raise ValueError("query 不能为空")
        payload = await SearchService.search_related_files(
            repo_id=repo_id,
            keywords=tokens,
            top_k=top_k,
        )
        return payload

    @staticmethod
    async def search_similar_code(repo_id: str, code_text: str, top_k: int = 10) -> Dict[str, object]:
        payload = await SearchService.search_similar_code(
            repo_id=repo_id,
            code_text=code_text,
            top_k=top_k,
        )
        return payload

    @staticmethod
    async def query_dependencies(
        repo_id: str,
        target_type: str,
        target: str,
        direction: str,
        limit: int = 20,
    ) -> Dict[str, object]:
        target_type = str(target_type or "").strip().lower()
        direction = str(direction or "").strip().lower()
        target = str(target or "").strip()
        if not target:
            raise ValueError("target 不能为空")
        with CodeGraphGateway.create_search() as search:
            if target_type == "file":
                if direction == "dependents":
                    res = await search.query_dependents_of_file(repo_id=repo_id, file_path=target)
                elif direction == "depended":
                    res = await search.query_dependented_of_file(repo_id=repo_id, file_path=target)
                else:
                    raise ValueError("file 类型 direction 仅支持 dependents/depended")
            elif target_type == "symbol":
                if direction == "callers":
                    res = await search.query_callers_of_symbol(repo_id=repo_id, symbol=target, limit=limit)
                elif direction == "callees":
                    res = await search.query_callees_of_symbol(repo_id=repo_id, symbol=target, limit=limit)
                else:
                    raise ValueError("symbol 类型 direction 仅支持 callers/callees")
            else:
                raise ValueError("target_type 仅支持 file/symbol")
        return {
            "repo_id": repo_id,
            "target_type": target_type,
            "target": target,
            "direction": direction,
            "result": bool(res.result),
            "message": str(res.message or ""),
            "content": dict(res.content or {}),
        }

    @staticmethod
    async def search_patterns(repo_id: str, query: str, top_k: int = 10) -> Dict[str, object]:
        return await SearchService.search_patterns(
            repo_id=repo_id, query=query, top_k=top_k,
        )

    @staticmethod
    async def trigger_reanalyze(workspace_path: str, user_id: str, force: bool = False) -> AnalyzeResult:
        ready = await AutoAnalyzeOrchestrator.ensure_workspace_ready(
            workspace_path=workspace_path,
            user_id=user_id,
            force=force,
        )
        return AnalyzeResult(
            ok=ready.ok,
            repo_id=ready.repo_id,
            status=ready.status,
            message=ready.message,
            data=ready.details,
        )
