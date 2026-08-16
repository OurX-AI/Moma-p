import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict,Optional
from sqlalchemy import case,delete,select,update
from ..models.analysis_status import FileAnalysisStatus,RepoFileAnalysisState
from ...repo_mgmt.models.git_repo_mgmt import GitRepository
from .codeast.ast_analyzer import FileAstAnalyzer
from .codechunk.code_chunk import CodeChunkService
from .codegraph.gateway import CodeGraphGateway
from .codevector.code_vector import CodeVectorService
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from app.utils.common import normalize_path, strip_utf8_bom


class FileAnalysisService:
    """从 RepoFileAnalysisState 抢占待处理记录，执行单文件切片/AST/向量化等实际分析。"""

    _MAX_CONCURRENT_REPO_POOLS = 2 # 最大并发仓库分析线程池数量
    _repo_pool_semaphore: Optional[asyncio.Semaphore] = None
    # 全局调度循环相关
    _scheduler_task: Optional[asyncio.Task] = None
    _scheduler_stop_event: Optional[asyncio.Event] = None
    # 仓库分析线程池相关
    _running_tasks: Dict[str, asyncio.Task] = {}

    @staticmethod
    def start_global_scheduler(
        interval_seconds: float = 2.0,
        worker_count: Optional[int] = None,
    ) -> bool:
        """启动全局调度循环。
        Args:
            interval_seconds: 调度间隔时间(秒)。
            worker_count: 每仓worker数量；None 时使用 settings.code_analysis_file_worker_count。
        Returns:
            bool: 是否启动成功。
        """
        if worker_count is None:
            worker_count = settings.code_analysis_file_worker_count
        scheduler_task = FileAnalysisService._scheduler_task
        if scheduler_task and not scheduler_task.done(): # 如果调度任务正在运行，则返回False
            return False
        
        FileAnalysisService._scheduler_stop_event = asyncio.Event() # 设置停止事件
        FileAnalysisService._scheduler_task = asyncio.create_task(
            FileAnalysisService._scheduler_loop(
                interval_seconds=interval_seconds,
                worker_count=worker_count,
            )
        )
        logging.info("文件分析调度器已启动 worker_count=%s interval=%ss", worker_count, interval_seconds)
        return True

    @staticmethod
    async def stop_global_scheduler() -> None:
        """停止全局调度循环。"""
        stop_event = FileAnalysisService._scheduler_stop_event
        scheduler_task = FileAnalysisService._scheduler_task
        # 设置停止事件
        if stop_event:
            stop_event.set()
        # 取消调度任务
        if scheduler_task and not scheduler_task.done(): # 如果调度任务正在运行，则取消
            scheduler_task.cancel()
            try:
                await scheduler_task # 等待调度任务完成
            except (asyncio.CancelledError, RuntimeError):
                # RuntimeError: Future attached to a different loop（跨场景 session 切换）
                pass
        # 清理停止事件和调度任务
        FileAnalysisService._scheduler_stop_event = None
        FileAnalysisService._scheduler_task = None

    @staticmethod
    async def _scheduler_loop(
        interval_seconds: float,
        worker_count: int,
    ) -> None:
        """全局调度循环。
        Args:
            interval_seconds: 调度间隔时间(秒)。
            worker_count: 每仓worker数量。
        """
        chunk_on = bool(settings.code_analysis_line_chunk_enabled)
        summary_on = bool(settings.code_analysis_symbol_summary_enabled)
        if not chunk_on and not summary_on:
            return

        poll_interval = max(interval_seconds, 120)
        while True:
            try:
                stop_event = FileAnalysisService._scheduler_stop_event
                if stop_event and stop_event.is_set():
                    return

                repo_ids = await FileAnalysisService._list_repos_need_analysis(chunk_on, summary_on)
                for repo_id in repo_ids:
                    await FileAnalysisService.start_repo_analysis(
                        repo_id=repo_id,
                        chunk_on=chunk_on,
                        summary_on=summary_on,
                        worker_count=worker_count,
                    )
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error("文件分析全局调度循环异常: %s", e)
                await asyncio.sleep(poll_interval)

    @staticmethod
    async def _list_repos_need_analysis(chunk_on: bool = True, summary_on: bool = True) -> list[str]:
        async with get_db_session() as db:
            rows = (
                await db.execute(
                    select(RepoFileAnalysisState.repo_id)
                    .where(RepoFileAnalysisState.needs_analysis_filter(
                        chunk_on=chunk_on,
                        summary_on=summary_on,
                    ))
                    .distinct()
                )
            ).all()
            return [str(row[0]) for row in rows if row and row[0]]
    
    @staticmethod
    async def start_repo_analysis(
        repo_id: str,
        chunk_on: bool,
        summary_on: bool,
        worker_count: int = 2,
    ) -> None:
        """幂等启动指定仓库的分析 worker 线程池。"""
        if not settings.codebase_enabled:
            return
        
        running_task = FileAnalysisService._running_tasks.get(repo_id)
        if running_task and not running_task.done():
            return

        async def worker_pool_runner() -> None:
            try:
                # 获取仓库分析线程池信号量
                async with FileAnalysisService._get_repo_pool_semaphore():
                    workers = [FileAnalysisService._worker_loop(repo_id, chunk_on, summary_on) for _ in range(max(worker_count, 1))]
                    # 启动worker协程并等待它们完成
                    await asyncio.gather(*workers)
            finally:
                FileAnalysisService._running_tasks.pop(repo_id, None)

        # 创建并记录运行中的任务
        running_task = asyncio.create_task(worker_pool_runner())
        FileAnalysisService._running_tasks[repo_id] = running_task

    @staticmethod
    async def stop_repo_analysis(
        repo_id: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        """停止指定仓库的文件分析 worker（用于删除分析数据前的并发保护）。"""
        running_task = FileAnalysisService._running_tasks.get(repo_id)
        if not running_task or running_task.done():
            FileAnalysisService._running_tasks.pop(repo_id, None)
            return

        running_task.cancel()
        try:
            await asyncio.wait_for(running_task, timeout=timeout_seconds)
        except (asyncio.CancelledError, Exception):
            pass

        FileAnalysisService._running_tasks.pop(repo_id, None)

    @staticmethod
    def _get_repo_pool_semaphore() -> asyncio.Semaphore:
        if FileAnalysisService._repo_pool_semaphore is None:
            FileAnalysisService._repo_pool_semaphore = asyncio.Semaphore(FileAnalysisService._MAX_CONCURRENT_REPO_POOLS)
        return FileAnalysisService._repo_pool_semaphore

    @staticmethod
    async def _worker_loop(
        repo_id: str,
        chunk_on: bool,
        summary_on: bool,
    ) -> None:
        idle_rounds = 0
        while True:
            record_id = await FileAnalysisService._get_one_record_and_mark_running(repo_id, chunk_on, summary_on)
            if not record_id:
                idle_rounds += 1
                if idle_rounds >= 3:
                    return
                await asyncio.sleep(0.3)
                continue
            idle_rounds = 0
            await FileAnalysisService._analysis_one_file(record_id, chunk_on, summary_on)

    @staticmethod
    async def _get_one_record_and_mark_running(
        repo_id: str,
        chunk_on: bool,
        summary_on: bool,
    ) -> Optional[str]:
        """抢占需要分析的记录，将所有待处理阶段标记为 RUNNING。返回记录 ID 或 None。"""
        async with get_db_session() as db:
            # 使用原子操作：先查询需要分析的记录，然后立即更新状态
            # 这样可以防止多个 worker 同时处理同一个文件
            filter_condition = RepoFileAnalysisState.needs_analysis_filter(chunk_on=chunk_on, summary_on=summary_on)

            # 查询一个需要分析的记录
            state = await db.scalar(
                select(RepoFileAnalysisState)
                .where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    filter_condition,
                )
                .order_by(
                    # chunk 优先：需要 chunk 的排前面
                    case(
                        (RepoFileAnalysisState.chunk_status.in_([
                            FileAnalysisStatus.IDLE.value,
                            FileAnalysisStatus.FAILED.value,
                        ]), 0),
                        else_=1,
                    ).asc(),
                    RepoFileAnalysisState.updated_at.asc(),
                )
                .limit(1)
            )
            if not state:
                return None

            now = datetime.now()
            update_values = {"last_error": None}
            # 只标记第一个需要处理的阶段为 RUNNING，避免同时标记导致 worker 无法识别
            if state.needs_chunk and chunk_on:
                update_values["chunk_status"] = FileAnalysisStatus.RUNNING.value
                update_values["last_chunk_started_at"] = now
            elif state.needs_summary and summary_on:
                update_values["summary_status"] = FileAnalysisStatus.RUNNING.value
                update_values["last_summary_started_at"] = now
            if len(update_values) == 1:
                return None

            try:
                # 使用原子更新：在 WHERE 子句中再次检查状态，防止竞态条件
                updated = await db.execute(
                    update(RepoFileAnalysisState)
                    .where(
                        RepoFileAnalysisState.id == state.id,
                        RepoFileAnalysisState.needs_analysis_filter(
                            chunk_on=chunk_on,
                            summary_on=summary_on,
                        ),
                    )
                    .values(**update_values)
                )
                if (updated.rowcount or 0) == 0:
                    # 更新失败，说明其他 worker 已经处理了这个文件
                    await db.rollback()
                    return None
                await db.commit()
            except Exception as e:
                try:
                    await db.rollback()
                except Exception:
                    pass
                logging.warning(
                    "标记 repo_file_analysis_state 为 RUNNING 失败 repo_id=%s record_id=%s error=%s",
                    repo_id,
                    state.id,
                    e,
                )
                return None
            # 只返回 ID，实际 record 会在 _analysis_one_file 中重新加载
            return state.id

    @staticmethod
    async def _analysis_one_file(
        record_id: str,
        chunk_on: bool,
        summary_on: bool,
    ) -> None:
        if not record_id:
            return

        async with get_db_session() as db:
            # 从数据库加载记录，确保 record 属于当前 session
            record = await db.scalar(
                select(RepoFileAnalysisState).where(RepoFileAnalysisState.id == record_id)
            )
            if not record:
                return

            repo_id = record.repo_id
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo or not repo.local_path or not os.path.isdir(repo.local_path):
                await FileAnalysisService._finish_record(
                    db=db, record=record,
                    chunk_status=FileAnalysisStatus.FAILED.value if chunk_on else None,
                    summary_status=FileAnalysisStatus.FAILED.value if summary_on else None,
                    last_error="仓库不存在或本地路径不可访问",
                )
                return

            abs_file_path = os.path.join(repo.local_path, *record.file_path.split("/"))
            if not os.path.exists(abs_file_path):
                await FileAnalysisService.delete_file_analysis_data(
                    repo_id=repo_id,
                    rel_file_path=record.file_path,
                    force=True,
                )
                return

            try:
                from ...repo_mgmt.models.git_repo_mgmt import RepoKind

                kind = getattr(repo, "kind", None) or RepoKind.CODE
                if kind == RepoKind.LIB:
                    if not summary_on:
                        return

                    from ...lib_analysis.services.file_processor import LibFileProcessor
                    ok, err_detail = await LibFileProcessor.analyze_file(
                        repo_id=repo_id,
                        repo_path=repo.local_path,
                        rel_file_path=record.file_path,
                        abs_file_path=abs_file_path,
                    )
                    await FileAnalysisService._finish_record(
                        db=db, record=record,
                        chunk_status=None,
                        summary_status=FileAnalysisStatus.COMPLETED.value if ok else FileAnalysisStatus.FAILED.value,
                        last_error=None if ok else err_detail,
                    )
                    return

                # --- CODE 仓库 ---
                # 检查哪个阶段被标记为 running（由 _get_one_record_and_mark_running 设置）
                if record.chunk_status == FileAnalysisStatus.RUNNING.value and chunk_on:
                    # chunk 阶段正在运行，执行 embedding
                    ok, err_detail = await FileAnalysisService._analyze_embed_phase(
                        repo_id=repo_id,
                        repo_path=repo.local_path,
                        rel_file_path=record.file_path,
                        abs_file_path=abs_file_path,
                    )
                    await FileAnalysisService._finish_record(
                        db=db, record=record,
                        chunk_status=FileAnalysisStatus.COMPLETED.value if ok else FileAnalysisStatus.FAILED.value,
                        summary_status=None,
                        last_error=None if ok else err_detail,
                    )
                elif record.summary_status == FileAnalysisStatus.RUNNING.value and summary_on:
                    # summary 阶段正在运行，执行符号摘要
                    ok, err_detail = await FileAnalysisService._analyze_symbol_summary_phase(
                        repo_id=repo_id,
                        repo_path=repo.local_path,
                        rel_file_path=record.file_path,
                        abs_file_path=abs_file_path,
                    )
                    await FileAnalysisService._finish_record(
                        db=db, record=record,
                        chunk_status=None,
                        summary_status=FileAnalysisStatus.COMPLETED.value if ok else FileAnalysisStatus.FAILED.value,
                        last_error=None if ok else err_detail,
                    )
            except Exception as e:
                logging.error("文件分析失败 repo_id=%s file_path=%s error=%s", repo_id, record.file_path, e)
                await FileAnalysisService._finish_record(
                    db=db, record=record,
                    chunk_status=FileAnalysisStatus.FAILED.value if chunk_on else None,
                    summary_status=FileAnalysisStatus.FAILED.value if summary_on else None,
                    last_error=str(e),
                )

    @staticmethod
    async def _analyze_embed_phase(
        repo_id: str,
        repo_path: str,
        rel_file_path: str,
        abs_file_path: str,
    ) -> tuple[bool, Optional[str]]:
        """快路径：AST + 行块 embedding。返回 (ok, err, await_symbol)。"""
        try:
            source = strip_utf8_bom(Path(abs_file_path).read_text(encoding="utf-8", errors="ignore"))
            file_ext = os.path.splitext(abs_file_path)[1].lower()
            file_info = await FileAstAnalyzer(repo_path, abs_file_path).analyze_file(source=source)
            line_chunks = CodeChunkService.slice_file(abs_file_path, source_text=source)
            if file_info:
                symbol_chunks = CodeChunkService.slice_symbol_bodies(file_info, file_ext=file_ext)
                chunks = CodeChunkService.merge_chunks(line_chunks, symbol_chunks)
            else:
                chunks = line_chunks
            await CodeVectorService.vectorize_and_store_line_chunks(
                repo_id,
                rel_file_path,
                chunks,
            )
            return True, None
        except Exception as e:
            logging.error("文件 embedding 阶段失败 repo_id=%s file_path=%s error=%s", repo_id, rel_file_path, e)
            return False, str(e)

    @staticmethod
    async def _analyze_symbol_summary_phase(
        repo_id: str,
        repo_path: str,
        rel_file_path: str,
        abs_file_path: str,
    ) -> tuple[bool, Optional[str]]:
        """异步补齐：AST + 符号摘要向量。"""
        try:
            source = strip_utf8_bom(Path(abs_file_path).read_text(encoding="utf-8", errors="ignore"))
            file_info = await FileAstAnalyzer(repo_path, abs_file_path).analyze_file(source=source)
            if not file_info:
                return False, "文件信息分析失败"
            await CodeVectorService.vectorize_and_store_symbol_summaries(
                repo_id,
                rel_file_path,
                file_info,
            )
            return True, None
        except Exception as e:
            logging.error(
                "文件符号摘要阶段失败 repo_id=%s file_path=%s error=%s",
                repo_id,
                rel_file_path,
                e,
            )
            return False, str(e)

    @staticmethod
    async def _finish_record(
        db,
        record: RepoFileAnalysisState,
        chunk_status: Optional[str],
        summary_status: Optional[str],
        last_error: Optional[str],
    ) -> None:
        """更新指定阶段的状态和时间戳。"""
        now = datetime.now()
        if chunk_status:
            record.chunk_status = chunk_status
            record.last_chunk_finished_at = now
        if summary_status:
            record.summary_status = summary_status
            record.last_summary_finished_at = now
        record.last_error = last_error
        record.updated_at = now
        await db.commit()

    @staticmethod
    async def delete_file_analysis_data(
        repo_id: str,
        rel_file_path: str,
        force: bool = False,
    ) -> Dict[str, object]:
        normalized_file_path = normalize_path(rel_file_path).strip("/")
        async with get_db_session() as db:
            record = await db.scalar(
                select(RepoFileAnalysisState).where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.file_path == normalized_file_path,
                )
            )
            if record and not force and (
                record.chunk_status == FileAnalysisStatus.RUNNING.value
                or record.summary_status == FileAnalysisStatus.RUNNING.value
            ):
                raise ValueError("该文件分析任务正在运行，无法删除（可使用 force=true 强制删除）")
            
            # 删除文件分析状态记录
            deleted_states = await db.execute(
                delete(RepoFileAnalysisState).where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.file_path == normalized_file_path,
                )
            )
            await db.commit()

        # 删除向量记录（code / lib 分别清理）
        deleted_vectors = await CodeVectorService.delete_file_vector_records(
            repo_id=repo_id,
            rel_file_path=normalized_file_path,
        )
        try:
            from ...lib_analysis.services.api_vector import ApiVectorService

            deleted_vectors += await ApiVectorService.delete_file_vector_records(
                repo_id=repo_id,
                rel_file_path=normalized_file_path,
            )
        except Exception as e:
            logging.warning(
                "删除 Lib API 向量失败 repo_id=%s file_path=%s error=%s",
                repo_id,
                normalized_file_path,
                e,
            )

        # 删除 codegraph 中该文件对应数据
        if settings.code_graph_enabled:
            generator = None
            try:
                generator = CodeGraphGateway.create_generator(repo_id, "", "")
                await generator.delete_file_graph(normalized_file_path)
            except Exception as e:
                logging.warning("删除文件 codegraph 数据失败 repo_id=%s file_path=%s error=%s", repo_id, normalized_file_path, e)
            finally:
                if generator:
                    try:
                        generator.close()
                    except Exception:
                        pass
        try:
            from .nl2code_enhance.lexicon import RepoIdentifierLexicon

            RepoIdentifierLexicon.invalidate_repo(repo_id)
        except Exception:
            pass
        return {
            "repo_id": repo_id,
            "file_path": normalized_file_path,
            "deleted_file_states": int(deleted_states.rowcount or 0),
            "deleted_vector_records": int(deleted_vectors),
        }