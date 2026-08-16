from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Set
from sqlalchemy import func, select, update
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from ..models.analysis_status import (
    FileAnalysisStatus,
    RepoAnalysisStatus,
    RepoAnalysisTask,
    RepoFileAnalysisState,
)
from ..models.experience_status import RepoExperienceTask
from .analysis_service import AnalysisService
from .experience_service import ExperienceService
from .mr_experience.git_history_source import GitHistorySource
from .repo_path_ignore import RepoPathIgnore
from ...repo_mgmt.models.git_repo_mgmt import GitRepository, RepoKind


class IncrementalScanService:
    """
    后台 tick：对已登记（repo add）的 code/lib 仓做
    变更扫描、首次未完成扫描补跑、失败/卡住文件重处理。
    人工负责登记仓；启动交互 mcb 后由本服务周期处理。
    """

    _task: Optional[asyncio.Task] = None
    _stop_event: Optional[asyncio.Event] = None
    STALE_RUNNING_SEC = 1800

    @staticmethod
    def start(repo_path: Optional[str] = None) -> bool:
        if not settings.enable_incremental_scan:
            logging.info("增量扫描已关闭（ENABLE_INCREMENTAL_SCAN=false）")
            return False
            
        task = IncrementalScanService._task
        if task and not task.done():
            return False
        IncrementalScanService._stop_event = asyncio.Event()
        interval = max(float(settings.incremental_scan_interval_sec), 30.0)
        IncrementalScanService._task = asyncio.create_task(
            IncrementalScanService._loop(interval_seconds=interval, repo_path=repo_path)
        )
        scope = f"repo_path={repo_path}" if repo_path else "all registered repos"
        logging.info(
            "后台 tick 已启动：已登记仓的变更/补扫 + 失败重处理 interval=%ss scope=%s",
            interval,
            scope,
        )
        return True

    @staticmethod
    async def stop() -> None:
        stop_event = IncrementalScanService._stop_event
        if stop_event:
            stop_event.set()
        task = IncrementalScanService._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        IncrementalScanService._stop_event = None
        IncrementalScanService._task = None

    @staticmethod
    async def _loop(interval_seconds: float, repo_path: Optional[str] = None) -> None:
        await IncrementalScanService.run_once(repo_path=repo_path)
        while True:
            try:
                stop_event = IncrementalScanService._stop_event
                if stop_event and stop_event.is_set():
                    return
                await asyncio.sleep(interval_seconds)
                await IncrementalScanService.run_once(repo_path=repo_path)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error("增量扫描循环异常: %s", e)

    @staticmethod
    async def run_once(repo_path: Optional[str] = None) -> None:
        """repo_path 非空时只扫该仓；为空时扫所有已登记仓。"""
        async with get_db_session() as db:
            if repo_path:
                repo = await db.scalar(
                    select(GitRepository).where(GitRepository.local_path == repo_path)
                )
                repos = [repo] if repo else []
            else:
                repos = (await db.scalars(select(GitRepository))).all()
        for repo in repos:
            kind = (getattr(repo, "kind", None) or RepoKind.CODE).strip().lower()
            if kind not in (RepoKind.CODE, RepoKind.LIB):
                continue
            if not repo.local_path or not os.path.isdir(repo.local_path):
                continue
            try:
                # 恢复卡住的文件分析
                await IncrementalScanService._recover_stale_running(repo.id)
                # 触发增量分析
                if not await IncrementalScanService._is_scan_active(repo.id):
                    if await IncrementalScanService._needs_rescan(repo):
                        await AnalysisService.start_scan(repo_id=repo.id)
                        logging.info(
                            "增量扫描触发变更分析 repo_id=%s path=%s kind=%s",
                            repo.id,
                            repo.local_path,
                            kind,
                        )
            except Exception as e:
                logging.warning("增量扫描触发失败 repo_id=%s error=%s", repo.id, e)

            # 触发 MR 经验分析
            await IncrementalScanService._maybe_trigger_experience_analyze(repo)

    @staticmethod
    async def _recover_stale_running(repo_id: str) -> int:
        """把长时间卡在 RUNNING 的文件重置为 IDLE，便于失败重处理。"""
        cutoff_dt = datetime.now() - timedelta(seconds=IncrementalScanService.STALE_RUNNING_SEC)
        async with get_db_session() as db:
            # 恢复卡住的 chunk 阶段
            result_chunk = await db.execute(
                update(RepoFileAnalysisState)
                .where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.chunk_status == FileAnalysisStatus.RUNNING.value,
                    RepoFileAnalysisState.last_chunk_started_at.is_not(None),
                    RepoFileAnalysisState.last_chunk_started_at < cutoff_dt,
                )
                .values(
                    chunk_status=FileAnalysisStatus.IDLE.value,
                    last_error="stale running recovered by background tick",
                )
            )
            # 恢复卡住的 summary 阶段
            result_summary = await db.execute(
                update(RepoFileAnalysisState)
                .where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.summary_status == FileAnalysisStatus.RUNNING.value,
                    RepoFileAnalysisState.last_summary_started_at.is_not(None),
                    RepoFileAnalysisState.last_summary_started_at < cutoff_dt,
                )
                .values(
                    summary_status=FileAnalysisStatus.IDLE.value,
                    last_error="stale running recovered by background tick",
                )
            )
            await db.commit()
            return int(result_chunk.rowcount or 0) + int(result_summary.rowcount or 0)

    @staticmethod
    async def _is_scan_active(repo_id: str) -> bool:
        running = AnalysisService._running_scan_tasks.get(repo_id)
        if running and not running.done():
            return True
        async with get_db_session() as db:
            status = await db.scalar(
                select(RepoAnalysisTask.scan_status).where(RepoAnalysisTask.repo_id == repo_id)
            )
        return status == RepoAnalysisStatus.RUNNING.value

    @staticmethod
    async def _needs_rescan(repo: GitRepository) -> bool:
        extensions = AnalysisService.CODE_EXTENSIONS
        async with get_db_session() as db:
            task = await db.scalar(
                select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo.id)
            )
            db_file_count = await db.scalar(
                select(func.count())
                .select_from(RepoFileAnalysisState)
                .where(RepoFileAnalysisState.repo_id == repo.id)
            )
        if not task or not task.last_scan_finished_at:
            # 已登记但尚未成功扫完：由后台 tick 拉起（含首次）；人工只需 repo add
            return True

        from .scan_change_detector import ScanChangeDetector

        git_decision = ScanChangeDetector.needs_rescan_by_git(
            repo.id,
            repo.local_path,
            extensions,
        )
        if git_decision is not None:
            return git_decision

        disk_count = IncrementalScanService._count_source_files(repo.local_path, extensions)
        if int(db_file_count or 0) != disk_count:
            return True
        latest_mtime = IncrementalScanService._max_source_mtime(repo.local_path, extensions)
        if latest_mtime is None:
            return False
        return latest_mtime > task.last_scan_finished_at

    @staticmethod
    async def _maybe_trigger_experience_analyze(repo: GitRepository) -> None:
        """触发 MR 经验分析。
        如果无法收集新条目（首次检查 job running / last_sha / has_new_entries），则跳过。
        """
        if not settings.mr_experience_enabled:
            return
            
        kind = (getattr(repo, "kind", None) or RepoKind.CODE).strip().lower()
        if kind != RepoKind.CODE:
            return
        if not repo.local_path or not os.path.isdir(repo.local_path):
            return
        try:
            if await ExperienceService.is_job_running(repo.id):
                return  # 无新 commit，跳过

            last_sha = await IncrementalScanService._get_last_collected_sha(repo.id)
            if last_sha is None:
                # 首次：拉最近 lookback_days 天内的 commit（最多 max_collect_per_run 条）
                since_date = (datetime.now() - timedelta(days=settings.mr_experience_lookback_days)).date().isoformat()
                await ExperienceService.start_analyze(
                    repo.id,
                    since=since_date,
                    limit=settings.mr_experience_max_collect_per_run,
                )
                logging.info(
                    "增量扫描触发 MR 经验分析（首次）repo_id=%s path=%s since=%s limit=%s",
                    repo.id,
                    repo.local_path,
                    since_date,
                    settings.mr_experience_max_collect_per_run,
                )
            else:
                # 增量：从上次 collect SHA 开始拉最近 max_collect_per_run 条
                if not GitHistorySource.has_new_entries(repo.local_path, after_sha=last_sha):
                    return
                
                # 触发 MR 经验分析
                await ExperienceService.start_analyze(
                    repo.id,
                    after_sha=last_sha,
                    limit=settings.mr_experience_max_collect_per_run,
                )
                logging.info(
                    "增量扫描触发 MR 经验分析（增量）repo_id=%s path=%s after_sha=%s limit=%s",
                    repo.id,
                    repo.local_path,
                    last_sha[:10],
                    settings.mr_experience_max_collect_per_run,
                )
        except Exception as e:
            logging.warning("增量 MR 经验分析触发失败 repo_id=%s error=%s", repo.id, e)

    @staticmethod
    async def _get_last_collected_sha(repo_id: str) -> Optional[str]:
        async with get_db_session() as db:
            task = await db.scalar(
                select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id)
            )
            return task.last_collected_commit_sha if task else None

    @staticmethod
    def _count_source_files(repo_root: str, extensions: Set[str]) -> int:
        ignorer = RepoPathIgnore.load(
            repo_root,
            builtin_dir_names=AnalysisService.EXCLUDED_DIRS,
        )
        count = 0
        for parent_root, dirs, files in AnalysisService._iter_scan_directories(repo_root, None):
            ignorer.filter_walk_dirs(parent_root, dirs)
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext not in extensions:
                    continue
                abs_path = os.path.join(parent_root, name)
                rel = os.path.relpath(abs_path, repo_root)
                if ignorer.should_ignore_file(rel):
                    continue
                count += 1
        return count

    @staticmethod
    def _max_source_mtime(repo_root: str, extensions: Set[str]) -> Optional[datetime]:
        ignorer = RepoPathIgnore.load(
            repo_root,
            builtin_dir_names=AnalysisService.EXCLUDED_DIRS,
        )
        latest: Optional[datetime] = None
        for parent_root, dirs, files in AnalysisService._iter_scan_directories(repo_root, None):
            ignorer.filter_walk_dirs(parent_root, dirs)
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext not in extensions:
                    continue
                abs_path = os.path.join(parent_root, name)
                rel = os.path.relpath(abs_path, repo_root)
                if ignorer.should_ignore_file(rel):
                    continue
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(abs_path))
                except OSError:
                    continue
                if latest is None or mtime > latest:
                    latest = mtime
        return latest
