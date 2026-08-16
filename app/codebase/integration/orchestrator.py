import logging
import os
import time
from typing import Optional
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from .dto import ReadyResult
from .errors import AnalyzeStartError, RepoRegistrationError, WorkspacePathError
from ..repo_analysis.services.analysis_service import AnalysisService
from ..repo_analysis.models.analysis_status import RepoAnalysisStatus
from ..repo_mgmt.models.git_repo_mgmt import RepoKind
from ..repo_mgmt.services.git_repo_service import GitRepositoryService
from ..repo_mgmt.services.repo_resolver import RepoResolver


class AutoAnalyzeOrchestrator:
    _last_trigger_ts: dict[str, float] = {}
    _cooldown_seconds = 600

    @classmethod
    def _normalize_workspace_path(cls, workspace_path: str) -> str:
        normalized = RepoResolver.normalize_repo_path(workspace_path)
        if not normalized or not os.path.isdir(normalized):
            raise WorkspacePathError(f"工作区不存在或不可访问: {workspace_path}")
        return normalized

    @classmethod
    async def _get_or_create_repo(cls, workspace_path: str, user_id: str) -> tuple[str, str]:
        normalized_path = cls._normalize_workspace_path(workspace_path)
        async with get_db_session() as db:
            repo = await RepoResolver.get_by_path(db, normalized_path)
            if repo is not None:
                return repo.id, normalized_path
            name = os.path.basename(normalized_path.rstrip("/")) or "workspace"
            try:
                created = await GitRepositoryService.create_repository_from_path(
                    session=db,
                    user_id=user_id or "cli",
                    name=name,
                    description=f"workspace auto registered: {name}",
                    local_repo_path=normalized_path,
                    git_url="",
                    kind=RepoKind.CODE,
                )
            except Exception as exc:
                raise RepoRegistrationError(str(exc)) from exc
            return created.id, normalized_path

    @classmethod
    async def ensure_workspace_ready(
        cls,
        workspace_path: str,
        user_id: str,
        *,
        force: bool = False,
    ) -> ReadyResult:
        repo_id, normalized_path = await cls._get_or_create_repo(workspace_path, user_id)
        scan_status = await AnalysisService.get_scan_status(repo_id=repo_id)
        current_status = str(scan_status.get("scan_status") or "")
        if current_status == RepoAnalysisStatus.RUNNING.value:
            return ReadyResult(
                ok=True,
                repo_id=repo_id,
                repo_path=normalized_path,
                status=current_status,
                message="当前扫描进行中，跳过重复启动。",
                details=scan_status,
            )
        now = time.time()
        last = cls._last_trigger_ts.get(repo_id, 0.0)
        if not force and now - last < cls._cooldown_seconds:
            return ReadyResult(
                ok=True,
                repo_id=repo_id,
                repo_path=normalized_path,
                status="cooldown",
                message="最近已触发分析，跳过重复启动。",
            )
        try:
            scan_result = await AnalysisService.start_scan(repo_id=repo_id)
        except Exception as exc:
            raise AnalyzeStartError(str(exc)) from exc
        cls._last_trigger_ts[repo_id] = now
        status = str(scan_result.get("scan_status") or "")
        logging.info(
            "workspace auto analyze triggered repo_id=%s status=%s path=%s",
            repo_id,
            status,
            normalized_path,
        )
        return ReadyResult(
            ok=True,
            repo_id=repo_id,
            repo_path=normalized_path,
            status=status or "running",
            message="已触发项目自动扫描。",
            details=scan_result,
        )

    @classmethod
    async def ensure_workspace_registered(
        cls,
        workspace_path: str,
        user_id: str,
    ) -> ReadyResult:
        repo_id, normalized_path = await cls._get_or_create_repo(workspace_path, user_id)
        scan_status = await AnalysisService.get_scan_status(repo_id=repo_id)
        current_status = str(scan_status.get("scan_status") or "")
        return ReadyResult(
            ok=True,
            repo_id=repo_id,
            repo_path=normalized_path,
            status=current_status or "unknown",
            message="仓库已就绪，可直接查询。",
            details=scan_status,
        )

    @classmethod
    async def trigger_reanalyze(cls, workspace_path: str, user_id: str) -> ReadyResult:
        return await cls.ensure_workspace_ready(workspace_path, user_id, force=True)
