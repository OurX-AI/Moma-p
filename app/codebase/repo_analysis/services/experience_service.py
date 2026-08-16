from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional
from sqlalchemy import delete, func, select, update
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from ..models.experience_status import (
    ExperienceItemStatus,
    ExperienceJobStatus,
    MrExperienceItem,
    RepoExperienceTask,
)
from .mr_experience.change_filter import ChangeFilter
from .mr_experience.git_history_source import GitHistorySource
from .mr_experience.models import ExperiencePattern, FileChange
from .mr_experience.pattern_summarizer import (
    PatternSummarizer,
    PatternSummarizerError,
)
from .mr_experience.pattern_vector import PatternVectorService
from ...repo_mgmt.models.git_repo_mgmt import GitRepository, RepoKind


class ExperienceService:
    """历史合入经验沉淀编排：采集 → 规则筛选 → LLM → 向量（失败记 failed 并支持重试）。"""

    _running_jobs: Dict[str, asyncio.Task] = {}
    _retry_scheduler_task: Optional[asyncio.Task] = None
    _retry_stop_event: Optional[asyncio.Event] = None
    MAX_RETRY = 5
    DEFAULT_ANALYZE_LIMIT = 50

    @staticmethod
    async def is_job_running(repo_id: str) -> bool:
        running = ExperienceService._running_jobs.get(repo_id)
        if running and not running.done():
            return True

        # 内存无活跃 task，但 DB 标记 running → 僵尸状态，直接修复
        async with get_db_session() as db:
            task = await db.scalar(
                select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id)
            )
            if task and task.job_status == ExperienceJobStatus.RUNNING.value:
                task.job_status = ExperienceJobStatus.IDLE.value
                task.last_error = "job was orphaned (process restarted or task lost)"
                task.last_finished_at = datetime.now()
                await db.commit()
                asyncio.create_task(ExperienceService._refresh_counters(repo_id))
            return False

    @staticmethod
    async def start_analyze(
        repo_id: str,
        *,
        since: Optional[str] = None,
        after_sha: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, object]:
        if not settings.mr_experience_enabled:
            raise ValueError("MR 经验能力已关闭：请设置 MR_EXPERIENCE_ENABLED=true")
        
        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            
            # 只有是Code类型的仓库才能进行经验沉淀
            kind = getattr(repo, "kind", None) or RepoKind.CODE
            if kind != RepoKind.CODE:
                raise ValueError(f"experience 仅支持 kind=code，当前 kind={kind}")

            # 仓库本地路径不可用
            if not repo.local_path:
                raise ValueError("仓库本地路径不可用")
            local_path = repo.local_path

        # 检查是否已在运行（含内存+DB+超时判断）
        if await ExperienceService.is_job_running(repo_id):
            async with get_db_session() as db:
                task = await db.scalar(select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
            return ExperienceService._job_to_dict(task) if task else {"repo_id": repo_id, "job_status": "running"}

        # 设置 job 为 running
        async with get_db_session() as db:
            task = await db.scalar(select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
            if not task:
                task = RepoExperienceTask(repo_id=repo_id, job_status=ExperienceJobStatus.IDLE.value)
                db.add(task)
            task.job_status = ExperienceJobStatus.RUNNING.value
            task.last_error = None
            task.last_started_at = datetime.now()
            task.last_finished_at = None
            await db.commit()

        # 创建一个新任务并将其添加到 _running_jobs 字典中
        ExperienceService._running_jobs[repo_id] = asyncio.create_task(
            ExperienceService._run_analyze(repo_id, local_path, since=since, after_sha=after_sha, limit=limit)
        )

        # 启动30s MrExperienceItem 扫描处理调度器
        ExperienceService.ensure_retry_scheduler(repo_id=repo_id)

        # 返回运行状态
        return {
            "repo_id": repo_id,
            "job_status": ExperienceJobStatus.RUNNING.value,
            "info": "experience analyze started",
            "since": since,
            "after_sha": after_sha,
            "limit": limit,
        }

    @staticmethod
    async def _run_analyze(
        repo_id: str,
        local_path: str,
        *,
        since: Optional[str],
        after_sha: Optional[str],
        limit: int,
    ) -> None:
        try:
            # 回收上次崩溃遗留的MrExperienceItem的RUNNING任务
            async with get_db_session() as db:
                await db.execute(
                    update(MrExperienceItem)
                    .where(
                        MrExperienceItem.repo_id == repo_id,
                        MrExperienceItem.status == ExperienceItemStatus.RUNNING.value,
                    )
                    .values(
                        status=ExperienceItemStatus.PENDING.value,
                        last_error="orphaned running item recovered",
                    )
                )
                await db.commit()

            # 收集 git 历史
            entries = GitHistorySource.collect(local_path, since=since, after_sha=after_sha, limit=limit)
            created = 0
            async with get_db_session() as db:
                for entry in entries:
                    # 检查是否已经存在该 commit
                    existing = await db.scalar(
                        select(MrExperienceItem).where(
                            MrExperienceItem.repo_id == repo_id,
                            MrExperienceItem.commit_sha == entry.commit_sha,
                        )
                    )
                    # 过滤文件
                    selected = ChangeFilter.select(entry.files)
                    # 如果过滤后的文件为空，则跳过
                    if not selected:
                        continue
                    # 将过滤后的文件转换为 JSON 格式
                    files_json = json.dumps(
                        [
                            {
                                "path": f.path,
                                "status": f.status,
                                "additions": f.additions,
                                "deletions": f.deletions,
                            }
                            for f in selected
                        ],
                        ensure_ascii=False,
                    )
                    if existing:
                        # 如果MrExperienceItem已分析好了，直接返回
                        if existing.status == ExperienceItemStatus.READY.value:
                            continue
                        existing.commit_message = entry.message
                        existing.committed_at = entry.committed_at
                        existing.is_merge = 1 if entry.is_merge else 0
                        existing.candidate_files_json = files_json
                        # 如果MrExperienceItem分析失败了，则重置为PENDING状态
                        if existing.status == ExperienceItemStatus.FAILED.value:
                            existing.status = ExperienceItemStatus.PENDING.value
                            existing.retry_count = 0
                            existing.last_error = None
                    else:
                        db.add(
                            MrExperienceItem(
                                repo_id=repo_id,
                                commit_sha=entry.commit_sha,
                                commit_message=entry.message,
                                committed_at=entry.committed_at,
                                is_merge=1 if entry.is_merge else 0,
                                candidate_files_json=files_json,
                                status=ExperienceItemStatus.PENDING.value,
                            )
                        )
                        created += 1
                await db.commit()

            # 在RepoExperienceTask中记录分析的最新一次MR的信息
            if entries:
                newest = entries[0]
                async with get_db_session() as db:
                    task = await db.scalar(select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
                    if task:
                        task.last_collected_commit_sha = newest.commit_sha
                        task.last_collected_committed_at = newest.committed_at
                        await db.commit()

            # 处理MrExperienceItem
            await ExperienceService._process_pending_and_failed(
                repo_id,
                include_failed=False,
                max_items=settings.mr_experience_process_batch_size,
            )

            # 更新RepoExperienceTask中的MR分析计数状态
            await ExperienceService._refresh_counters(repo_id, job_status=ExperienceJobStatus.COMPLETED.value)
            logging.info(
                "experience analyze 完成 repo_id=%s created=%s collected=%s",
                repo_id,
                created,
                len(entries),
            )
        except Exception as e:
            logging.error("experience analyze 失败 repo_id=%s error=%s", repo_id, e)
            async with get_db_session() as db:
                task = await db.scalar(select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
                if task:
                    task.job_status = ExperienceJobStatus.FAILED.value
                    task.last_error = str(e)
                    task.last_finished_at = datetime.now()
                    await db.commit()
        finally:
            ExperienceService._running_jobs.pop(repo_id, None)

    @staticmethod
    async def _process_pending_and_failed(
        repo_id: str,
        *,
        include_failed: bool,
        max_items: Optional[int] = None,
    ) -> None:
        statuses = [ExperienceItemStatus.PENDING.value]
        
        # 如果需要包含失败的条目，则添加失败的条目状态
        if include_failed:
            statuses.append(ExperienceItemStatus.FAILED.value)
        
        processed = 0
        while True:
            if max_items is not None and processed >= max_items:
                return
            async with get_db_session() as db:
                item = await db.scalar(
                    select(MrExperienceItem)
                    .where(
                        MrExperienceItem.repo_id == repo_id,
                        MrExperienceItem.status.in_(statuses),
                        MrExperienceItem.retry_count < ExperienceService.MAX_RETRY,
                    )
                    .order_by(MrExperienceItem.updated_at.asc())
                    .limit(1)
                )
                if not item:
                    return
                
                item_id = item.id
                updated = await db.execute(
                    update(MrExperienceItem)
                    .where(
                        MrExperienceItem.id == item_id,
                        MrExperienceItem.status.in_(statuses),
                    )
                    .values(
                        status=ExperienceItemStatus.RUNNING.value,
                        last_started_at=datetime.now(),
                        last_error=None,
                    )
                )
                if (updated.rowcount or 0) == 0:
                    await db.rollback()
                    continue
                await db.commit()

            await ExperienceService._process_one_item(item_id)
            processed += 1
    
    @staticmethod
    async def _process_one_item(item_id: str) -> None:
        async with get_db_session() as db:
            item = await db.scalar(select(MrExperienceItem).where(MrExperienceItem.id == item_id))
            if not item:
                return
            
            repo_id = item.repo_id
            commit_sha = item.commit_sha
            message = item.commit_message or ""
            try:
                files_data = json.loads(item.candidate_files_json or "[]")
            except json.JSONDecodeError:
                files_data = []
            files = [
                FileChange(
                    path=str(x.get("path") or ""),
                    status=str(x.get("status") or "M"),
                    additions=int(x.get("additions") or 0),
                    deletions=int(x.get("deletions") or 0),
                )
                for x in files_data
                if isinstance(x, dict) and x.get("path")
            ]

        try:
            if not files:
                raise PatternSummarizerError("候选文件为空")
            
            result = await PatternSummarizer.summarize(message, files, commit_sha)
            if not result.extractable:
                # 如果MR经验提取失败，则将MrExperienceItem状态设置为SKIPPED
                async with get_db_session() as db:
                    item = await db.scalar(select(MrExperienceItem).where(MrExperienceItem.id == item_id))
                    if not item:
                        return
                    item.title = None
                    item.steps_json = json.dumps(
                        {"skip_reason": result.skip_reason},
                        ensure_ascii=False,
                    )
                    item.status = ExperienceItemStatus.SKIPPED.value
                    item.last_error = None
                    item.last_finished_at = datetime.now()
                    await db.commit()
                logging.info(
                    "经验条目跳过 item_id=%s sha=%s reason=%s",
                    item_id,
                    commit_sha[:10],
                    result.skip_reason,
                )
                return
            
            # 过滤掉质量分过低的经验条目
            kept_patterns = ExperienceService._keep_high_quality_patterns(result.patterns)
            if not kept_patterns:
                async with get_db_session() as db:
                    item = await db.scalar(select(MrExperienceItem).where(MrExperienceItem.id == item_id))
                    if not item:
                        return
                    item.title = None
                    item.steps_json = json.dumps(
                        {"skip_reason": "经验质量分过低，已丢弃"},
                        ensure_ascii=False,
                    )
                    item.status = ExperienceItemStatus.SKIPPED.value
                    item.last_error = None
                    item.last_finished_at = datetime.now()
                    await db.commit()
                return
            await PatternVectorService.upsert_patterns(repo_id, kept_patterns)

            # 如果MR经验提取成功，则将MrExperienceItem状态设置为READY
            async with get_db_session() as db:
                item = await db.scalar(select(MrExperienceItem).where(MrExperienceItem.id == item_id))
                if not item:
                    return
                first_title = kept_patterns[0].title
                item.title = first_title if len(kept_patterns) == 1 else f"{first_title} 等{len(kept_patterns)}条经验"
                item.steps_json = json.dumps(
                    {
                        "patterns": [p.to_payload() for p in kept_patterns],
                        "total_extracted": len(result.patterns),
                        "kept_after_quality_filter": len(kept_patterns),
                        "quality_threshold": float(settings.mr_experience_min_quality_score or 0.0),
                    },
                    ensure_ascii=False,
                )
                item.status = ExperienceItemStatus.READY.value
                item.last_error = None
                item.last_finished_at = datetime.now()
                await db.commit()
        except Exception as e:
            logging.warning("经验条目失败 item_id=%s error=%s", item_id, e)
            async with get_db_session() as db:
                item = await db.scalar(select(MrExperienceItem).where(MrExperienceItem.id == item_id))
                if not item:
                    return
                item.status = ExperienceItemStatus.FAILED.value
                item.last_error = str(e)
                item.retry_count = int(item.retry_count or 0) + 1
                item.last_finished_at = datetime.now()
                await db.commit()

    @staticmethod
    async def _refresh_counters(repo_id: str, job_status: Optional[str] = None) -> None:
        """刷新任务计数器"""
        async with get_db_session() as db:
            total = await db.scalar(
                select(func.count()).select_from(MrExperienceItem).where(MrExperienceItem.repo_id == repo_id)
            )
            ready = await db.scalar(
                select(func.count())
                .select_from(MrExperienceItem)
                .where(
                    MrExperienceItem.repo_id == repo_id,
                    MrExperienceItem.status == ExperienceItemStatus.READY.value,
                )
            )
            failed = await db.scalar(
                select(func.count())
                .select_from(MrExperienceItem)
                .where(
                    MrExperienceItem.repo_id == repo_id,
                    MrExperienceItem.status == ExperienceItemStatus.FAILED.value,
                )
            )
            skipped = await db.scalar(
                select(func.count())
                .select_from(MrExperienceItem)
                .where(
                    MrExperienceItem.repo_id == repo_id,
                    MrExperienceItem.status == ExperienceItemStatus.SKIPPED.value,
                )
            )
            task = await db.scalar(select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
            if not task:
                task = RepoExperienceTask(repo_id=repo_id)
                db.add(task)
            task.total_items = int(total or 0)
            task.ready_items = int(ready or 0)
            task.failed_items = int(failed or 0)
            task.skipped_items = int(skipped or 0)
            if job_status:
                task.job_status = job_status
                task.last_finished_at = datetime.now()
            await db.commit()

    @staticmethod
    def _keep_high_quality_patterns(patterns: List[ExperiencePattern]) -> List[ExperiencePattern]:
        threshold = float(settings.mr_experience_min_quality_score or 0.0)
        kept: List[ExperiencePattern] = []
        for p in patterns:
            score = float(p.quality_score or 0.0)
            if score >= threshold:
                kept.append(p)
        return kept

    @staticmethod
    async def get_status(repo_id: str) -> Dict[str, object]:
        async with get_db_session() as db:
            task = await db.scalar(select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
            if not task:
                return {
                    "repo_id": repo_id,
                    "job_status": ExperienceJobStatus.IDLE.value,
                    "total_items": 0,
                    "ready_items": 0,
                    "failed_items": 0,
                    "skipped_items": 0,
                }
            data = ExperienceService._job_to_dict(task)
            return data

    @staticmethod
    def _job_to_dict(task: RepoExperienceTask) -> Dict[str, object]:
        return {
            "repo_id": task.repo_id,
            "job_status": task.job_status,
            "last_error": task.last_error,
            "last_started_at": task.last_started_at.isoformat() if task.last_started_at else None,
            "last_finished_at": task.last_finished_at.isoformat() if task.last_finished_at else None,
            "total_items": int(task.total_items or 0),
            "ready_items": int(task.ready_items or 0),
            "failed_items": int(task.failed_items or 0),
            "skipped_items": int(task.skipped_items or 0),
        }

    @staticmethod
    async def clear(repo_id: str) -> None:
        running = ExperienceService._running_jobs.get(repo_id)
        if running and not running.done():
            running.cancel()
            try:
                await asyncio.wait_for(running, timeout=5)
            except Exception:
                pass
            ExperienceService._running_jobs.pop(repo_id, None)
        async with get_db_session() as db:
            await db.execute(delete(MrExperienceItem).where(MrExperienceItem.repo_id == repo_id))
            await db.execute(delete(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id))
            await db.commit()
        try:
            await PatternVectorService.delete_repo_patterns(repo_id)
        except Exception as e:
            logging.warning("清理经验向量失败 repo_id=%s error=%s", repo_id, e)

    @classmethod
    def ensure_retry_scheduler(cls, interval_seconds: float = 30.0, repo_id: Optional[str] = None) -> None:
        task = cls._retry_scheduler_task
        if task and not task.done():
            return
        cls._retry_stop_event = asyncio.Event()
        cls._retry_scheduler_task = asyncio.create_task(
            cls._retry_loop(interval_seconds=interval_seconds, repo_id=repo_id)
        )

    @classmethod
    async def stop_retry_scheduler(cls) -> None:
        if cls._retry_stop_event:
            cls._retry_stop_event.set()
        task = cls._retry_scheduler_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except Exception:
                pass
        cls._retry_scheduler_task = None
        cls._retry_stop_event = None

    @classmethod
    async def _retry_loop(cls, interval_seconds: float, repo_id: Optional[str] = None) -> None:
        while True:
            try:
                if cls._retry_stop_event and cls._retry_stop_event.is_set():
                    return
                if repo_id:
                    # 只处理指定 repo
                    if repo_id not in cls._running_jobs or cls._running_jobs[repo_id].done():
                        await cls._process_pending_and_failed(
                            repo_id,
                            include_failed=True,
                            max_items=settings.mr_experience_process_batch_size,
                        )
                        await cls._refresh_counters(repo_id)
                else:
                    # 处理所有有 PENDING/FAILED 的 repo
                    repo_ids = await cls._list_repos_with_pending_or_failed()
                    for rid in repo_ids:
                        if rid in cls._running_jobs and not cls._running_jobs[rid].done():
                            continue
                        await cls._process_pending_and_failed(
                            rid,
                            include_failed=True,
                            max_items=settings.mr_experience_process_batch_size,
                        )
                        await cls._refresh_counters(rid)
                await asyncio.sleep(max(interval_seconds, 5.0))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error("experience 重试调度异常: %s", e)
                await asyncio.sleep(max(interval_seconds, 5.0))

    @staticmethod
    async def _list_repos_with_pending_or_failed() -> List[str]:
        async with get_db_session() as db:
            rows = (
                await db.execute(
                    select(MrExperienceItem.repo_id)
                    .where(
                        MrExperienceItem.status.in_(
                            [
                                ExperienceItemStatus.PENDING.value,
                                ExperienceItemStatus.FAILED.value,
                            ]
                        ),
                        MrExperienceItem.retry_count < ExperienceService.MAX_RETRY,
                    )
                    .distinct()
                )
            ).all()
            return [str(r[0]) for r in rows if r and r[0]]
