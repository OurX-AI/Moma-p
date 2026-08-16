import asyncio
import logging
import os
from datetime import datetime
from typing import Dict,Iterable,List,Optional,Set,Tuple
from sqlalchemy import delete,func,or_,select,update
from sqlalchemy.exc import IntegrityError
from ..models.analysis_status import FileAnalysisStatus,RepoAnalysisStatus,RepoAnalysisTask,RepoFileAnalysisState
from ...repo_mgmt.models.git_repo_mgmt import GitRepository
from .codegraph.gateway import CodeGraphGateway
from .file_analysis_service import FileAnalysisService
from .codevector.code_vector import CodeVectorService
from .repo_path_ignore import RepoPathIgnore
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from app.utils.common import normalize_path


class RepoScanCancelled(Exception):
    """扫描协作取消（通过数据库的 scan_status 触发）。"""


class AnalysisService:
    """统一编排服务：扫描仓库并驱动文件级分析消费。"""

    _running_scan_tasks: Dict[str, asyncio.Task] = {}
    _running_graph_tasks: Dict[str, asyncio.Task] = {}
    CODE_EXTENSIONS = {
        ".py",
        ".java",
        ".go",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".hh",
        ".hxx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".rs",
    }
    EXCLUDED_DIRS = {
        "__pycache__",
        ".git",
        ".idea",
        ".vscode",
        "venv",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "target",
        ".pytest_cache",
        ".mypy_cache",
        ".coverage",
        "__tests__",
        "tests",
    }

    @staticmethod
    async def start_scan(
        repo_id: str,
        target_rel_path: Optional[str] = None,
    ) -> Dict[str, object]:
        """启动仓库扫描：先扫描仓库，再启动文件级分析 worker。
        Args:
            repo_id: 代码仓ID。
            target_rel_path: 目标文件或目录路径（相对仓库根）。
        Returns:
            Dict[str, object]: 扫描结果。
        """
        if not settings.codebase_enabled:
            return {
                "repo_id": repo_id,
                "repo_path": None,
                "scan_status": RepoAnalysisStatus.IDLE.value,
                "last_error": None,
                "last_scan_started_at": None,
                "last_scan_finished_at": None,
                "scan_heartbeat_at": None,
            }

        repo_path: Optional[str] = None
        repo_kind: str = "code"
        normalized_target_rel_path: Optional[str] = None
        is_directory: Optional[bool] = None

        async with get_db_session() as db:
            # 获取仓库信息
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            if not repo.local_path or not os.path.isdir(repo.local_path):
                raise ValueError("仓库本地路径不存在或不可访问")
            repo_path = repo.local_path
            repo_kind = (getattr(repo, "kind", None) or "code").strip().lower()
            
            # 获取扫描任务
            task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
            if not task:
                task = RepoAnalysisTask(
                    repo_id=repo_id,
                    scan_status=RepoAnalysisStatus.IDLE.value,
                )
                db.add(task)
                await db.commit()
            if task and task.scan_status == RepoAnalysisStatus.RUNNING.value:
                return AnalysisService._scan_task_to_dict(task, repo.local_path)

            # 加锁前记录是否曾成功扫过（用于图谱增量判断；加锁会清空 last_scan_finished_at）
            had_prior_scan = bool(task and task.last_scan_finished_at)
            
            # 解析目标路径类型
            if target_rel_path:
                is_directory, normalized_target_rel_path = AnalysisService._resolve_target_type(repo.local_path, target_rel_path)
            
            # 获取扫描锁
            acquired_run_lock = await AnalysisService._acquire_scan_lock(
                db=db,
                repo_id=repo_id,
                task=task,
            )
            if not acquired_run_lock:
                # 如果获取扫描锁失败，则返回当前任务状态
                if task:
                    return AnalysisService._scan_task_to_dict(task, repo.local_path)
                return {
                    "repo_id": repo_id,
                    "repo_path": repo.local_path,
                    "scan_status": RepoAnalysisStatus.RUNNING.value,
                    "last_error": None,
                    "last_scan_started_at": None,
                    "last_scan_finished_at": None,
                    "scan_heartbeat_at": None,
                }
        
        # 如果扫描任务正在运行，则返回当前任务状态
        if repo_id in AnalysisService._running_scan_tasks and not AnalysisService._running_scan_tasks[repo_id].done():
            # 如果获取扫描锁成功，则返回当前任务状态
            return {
                "repo_id": repo_id,
                "repo_path": repo_path,
                "scan_status": RepoAnalysisStatus.RUNNING.value,
                "target_rel_path": normalized_target_rel_path,
                "is_directory": is_directory,
                "info": "scan is running",
            }

        # 启动扫描任务
        scanning_task = asyncio.create_task(
            AnalysisService._run_scan(
                repo_id=repo_id,
                repo_path=repo_path or "",
                target_rel_path=normalized_target_rel_path,
                is_directory=is_directory or False,
                allowed_extensions=AnalysisService.CODE_EXTENSIONS,
            )
        )
        AnalysisService._running_scan_tasks[repo_id] = scanning_task

        # 图谱：首次全量 init；已有索引则增量 sync/update_files（仅 kind=code）
        if settings.code_graph_enabled and repo_kind == "code":
            AnalysisService._schedule_code_graph(
                repo_id=repo_id,
                repo_path=repo_path or "",
                scan_task=scanning_task,
                had_prior_scan=had_prior_scan,
            )

        return {
            "repo_id": repo_id,
            "repo_path": repo_path,
            "scan_status": RepoAnalysisStatus.RUNNING.value,
            "target_rel_path": normalized_target_rel_path,
            "is_directory": is_directory,
            "info": "scan is running",
        }

    @staticmethod
    def has_existing_graph_index(repo_path: str, *, had_prior_scan: bool = False) -> bool:
        """判断仓库是否已有可增量更新的图谱索引。"""
        if repo_path and os.path.isdir(os.path.join(repo_path, ".codegraph")):
            return True
        return bool(had_prior_scan)

    @staticmethod
    def _schedule_code_graph(
        *,
        repo_id: str,
        repo_path: str,
        scan_task: asyncio.Task,
        had_prior_scan: bool,
    ) -> None:
        existing_graph_task = AnalysisService._running_graph_tasks.get(repo_id)
        if existing_graph_task and not existing_graph_task.done():
            return

        incremental = AnalysisService.has_existing_graph_index(
            repo_path,
            had_prior_scan=had_prior_scan,
        )

        async def _run_graph() -> None:
            try:
                await AnalysisService._run_code_graph(
                    repo_id=repo_id,
                    repo_path=repo_path,
                    incremental=incremental,
                )
            except Exception as e:
                logging.warning("代码图谱更新失败 repo_id=%s error=%s", repo_id, e)

        AnalysisService._running_graph_tasks[repo_id] = asyncio.create_task(_run_graph())

    @staticmethod
    async def _run_code_graph(
        *,
        repo_id: str,
        repo_path: str,
        incremental: bool,
    ) -> None:
        """全量 generate_graph，或增量 refresh_graph（基于 manifest 对比变更）。"""
        generator = None
        try:
            generator = CodeGraphGateway.create_generator(
                repo_id=repo_id,
                repo_name=str(repo_id),
                repo_local_path=repo_path or "",
            )
            provider_name = CodeGraphGateway.get_provider().name
            if not incremental:
                logging.info("CodeGraph 全量生成 provider=%s repo_id=%s", provider_name, repo_id)
                await generator.generate_graph(clean_stale=True)
                return

            logging.info("CodeGraph 增量刷新 provider=%s repo_id=%s", provider_name, repo_id)
            await generator.refresh_graph()
        finally:
            if generator:
                try:
                    generator.close()
                except Exception:
                    pass

    @staticmethod
    async def _assert_scan_is_running(db, repo_id: str) -> None:
        """如果上层已把 scan_status 从 RUNNING 改掉，则尽快停止扫描。"""
        status_value = await db.scalar(
            select(RepoAnalysisTask.scan_status).where(RepoAnalysisTask.repo_id == repo_id)
        )
        if status_value != RepoAnalysisStatus.RUNNING.value:
            raise RepoScanCancelled("scan cancelled by scan_status change")
    
    @staticmethod
    async def _acquire_scan_lock(
        db,
        repo_id: str,
        task: Optional[RepoAnalysisTask],
    ) -> bool:
        payload = {
            "last_error": None,
            "last_scan_started_at": datetime.now(),
            "last_scan_finished_at": None,
            "scan_heartbeat_at": datetime.now(),
        }

        # 如果扫描任务不存在，则创建扫描任务
        if task is None:
            try:
                db.add(RepoAnalysisTask(
                    repo_id=repo_id,
                    scan_status=RepoAnalysisStatus.RUNNING.value,
                    **payload,
                ))
                await db.commit()
                return True
            except IntegrityError:
                await db.rollback()
        
        # 如果扫描任务存在，则更新扫描任务状态为运行中
        updated = await db.execute(
            update(RepoAnalysisTask)
            .where(
                RepoAnalysisTask.repo_id == repo_id,
                RepoAnalysisTask.scan_status.in_([
                    RepoAnalysisStatus.IDLE.value,
                    RepoAnalysisStatus.COMPLETED.value,
                    RepoAnalysisStatus.FAILED.value,
                ]),
            )
            .values(
                scan_status=RepoAnalysisStatus.RUNNING.value,
                **payload,
            )
        )
        await db.commit()
        return (updated.rowcount or 0) > 0

    @staticmethod
    async def _run_scan(
        repo_id: str,
        repo_path: str,
        target_rel_path: Optional[str],
        is_directory: bool,
        allowed_extensions: Optional[Set[str]] = None,
    ) -> None:
        extensions = allowed_extensions or AnalysisService.CODE_EXTENSIONS
        try:
            async with get_db_session() as db:
                await AnalysisService._assert_scan_is_running(db, repo_id)
            
            if target_rel_path is not None:
                target_rel_path = normalize_path(target_rel_path.strip())
            
            if target_rel_path is not None and not is_directory:
                scanned_count = 0
                async with get_db_session() as db:
                    abs_path = os.path.normpath(os.path.join(repo_path, *target_rel_path.split("/")))
                    ok = await AnalysisService.update_file_state(
                        db, repo_id, abs_path, target_rel_path, allowed_extensions=extensions
                    )
                    if ok:
                        await AnalysisService._touch_scan_heartbeat(db, repo_id)
                    await db.commit()
                    scanned_count = 1 if ok else 0
            else:
                scanned_count, excluded_dirs = await AnalysisService._scan_dir_and_update_file_states(
                    repo_id=repo_id,
                    repo_root=repo_path,
                    target_rel_path=target_rel_path,
                    allowed_extensions=extensions,
                )

                # 删除排除的子目录下历史状态
                await AnalysisService._delete_files_under_excluded_dirs(
                    repo_id=repo_id,
                    excluded_dirs=excluded_dirs,
                )
            
            if target_rel_path and scanned_count == 0:
                raise ValueError("未匹配到需要重分析的文件")
            
            await AnalysisService._finish_scan_task(
                repo_id=repo_id,
                status=RepoAnalysisStatus.COMPLETED.value,
                last_error=None,
            )
        except RepoScanCancelled as e:
            logging.info("repo扫描已取消 repo_id=%s error=%s", repo_id, e)
            await AnalysisService._finish_scan_task(
                repo_id=repo_id,
                status=RepoAnalysisStatus.FAILED.value,
                last_error="scan cancelled",
            )
        except Exception as e:
            logging.error("repo扫描失败 repo_id=%s error=%s", repo_id, e)
            await AnalysisService._finish_scan_task(
                repo_id=repo_id,
                status=RepoAnalysisStatus.FAILED.value,
                last_error=str(e),
            )
        finally:
            AnalysisService._running_scan_tasks.pop(repo_id, None)

    @staticmethod
    async def _scan_dir_and_update_file_states(
        repo_id: str,
        repo_root: str,
        target_rel_path: Optional[str],
        allowed_extensions: Optional[Set[str]] = None,
    ) -> Tuple[int, Set[str]]:
        """扫描并更新文件级分析状态。
        Args:
            repo_id: 代码仓ID。
            repo_root: 仓库根路径。
            target_rel_path: 目标文件或目录路径（相对仓库根）。
        Returns:
            Tuple[int, Set[str]]: 本次成功扫描到的代码文件数量；剪枝掉的排除子目录相对路径集合（用于删除其下历史状态，不占全量路径内存）。
        """
        extensions = allowed_extensions or AnalysisService.CODE_EXTENSIONS
        scanned_code_files = 0
        excluded_dirs: Set[str] = set()
        ignorer = RepoPathIgnore.load(
            repo_root,
            builtin_dir_names=AnalysisService.EXCLUDED_DIRS,
        )
        async with get_db_session() as db:
            batch = 0
            
            # 迭代扫描目录
            for parent_root, dirs, files in AnalysisService._iter_scan_directories(repo_root=repo_root, target_rel_path=target_rel_path):
                await AnalysisService._assert_scan_is_running(db, repo_id)
                excluded_dirs.update(ignorer.filter_walk_dirs(parent_root, dirs))
                
                # 处理文件
                direct_file_paths: Set[str] = set()
                for filename in files:
                    abs_path = os.path.join(parent_root, filename)
                    rel_path = normalize_path(os.path.relpath(abs_path, repo_root))
                    if ignorer.should_ignore_file(rel_path):
                        continue
                    ok = await AnalysisService.update_file_state(
                        db, repo_id, abs_path, rel_path, allowed_extensions=extensions
                    )
                    if not ok:
                        continue
                    direct_file_paths.add(rel_path)
                    scanned_code_files += 1
                    batch += 1
                    if batch >= 200:
                        await AnalysisService._touch_scan_heartbeat(db, repo_id)
                        await db.commit()
                        batch = 0
                    
                # 删除目录中缺失的文件级分析状态记录
                await AnalysisService._delete_missing_files_in_cur_dir(
                    db=db,
                    repo_id=repo_id,
                    repo_root=repo_root,
                    cur_dir=parent_root,
                    existing_files=direct_file_paths,
                )
        
            if batch > 0:
                await AnalysisService._touch_scan_heartbeat(db, repo_id)
                await db.commit()
        return scanned_code_files, excluded_dirs

    @staticmethod
    def _iter_scan_directories(
        repo_root: str,
        target_rel_path: Optional[str],
    ) -> Iterable[tuple[str, list[str], list[str]]]:
        """迭代扫描目录。
        Args:
            repo_root: 仓库根路径。
            target_rel_path: 目标文件或目录路径（相对仓库根）。
        Returns:
            Iterable[tuple[str, list[str], list[str]]]: 迭代器，每个元素为(当前根目录, 目录列表, 文件列表)。
        """
        if not target_rel_path:
            yield from os.walk(repo_root)
            return
        
        abs_target = os.path.normpath(os.path.join(repo_root, *target_rel_path.split("/")))
        if not os.path.isdir(abs_target):
            return
        yield from os.walk(abs_target)

    @staticmethod
    def _should_refresh_state(
        state: RepoFileAnalysisState,
        file_modified_at: datetime,
    ) -> bool:
        latest_finished = max(
            filter(None, [state.last_chunk_finished_at, state.last_summary_finished_at]),
            default=None,
        )
        if latest_finished is None:
            return True
        return file_modified_at > latest_finished

    @staticmethod
    async def update_file_state(
        db,
        repo_id: str,
        abs_file_path: str,
        rel_file_path: str,
        allowed_extensions: Optional[Set[str]] = None,
    ) -> bool:
        if not os.path.isfile(abs_file_path):
            await db.execute(
                delete(RepoFileAnalysisState).where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.file_path == rel_file_path,
                )
            )
            return False
        
        # 过滤非代码文件    
        ext = os.path.splitext(abs_file_path)[1].lower()
        extensions = allowed_extensions or AnalysisService.CODE_EXTENSIONS
        if ext not in extensions:
            await db.execute(
                delete(RepoFileAnalysisState).where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.file_path == rel_file_path,
                )
            )
            return False
        
        # 更新文件级分析状态
        record = await db.scalar(
            select(RepoFileAnalysisState).where(
                RepoFileAnalysisState.repo_id == repo_id,
                RepoFileAnalysisState.file_path == rel_file_path,
            )
        )
        if record is None:
            db.add(RepoFileAnalysisState(
                repo_id=repo_id,
                file_path=rel_file_path,
                chunk_status=FileAnalysisStatus.IDLE.value,
                summary_status=FileAnalysisStatus.IDLE.value,
            ))
        else:
            file_modified_at = datetime.fromtimestamp(os.path.getmtime(abs_file_path))
            if AnalysisService._should_refresh_state(record, file_modified_at):
                record.chunk_status = FileAnalysisStatus.IDLE.value
                record.summary_status = FileAnalysisStatus.IDLE.value
                record.last_error = None
        return True

    @staticmethod
    async def _touch_scan_heartbeat(
        db,
        repo_id: str,
    ) -> None:
        """更新扫描心跳。"""
        task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
        if task:
            task.scan_heartbeat_at = datetime.now()

    @staticmethod
    async def _delete_missing_files_in_cur_dir(
        db,
        repo_id: str,
        repo_root: str,
        cur_dir: str,
        existing_files: Set[str],
    ) -> None:
        """删除目录中缺失文件的分析数据（状态 + 向量 + graph）。
        Args:
            db: 数据库会话（仅用于查询缺失路径；清理走 delete_file_analysis_data）。
            repo_id: 代码仓ID。
            repo_root: 仓库根路径。
            cur_dir: 当前目录绝对路径。
            existing_files: 磁盘上仍存在的相对路径集合。
        """
        rel_dir = normalize_path(os.path.relpath(cur_dir, repo_root))
        if rel_dir == ".":
            rel_dir = ""
        
        if rel_dir:
            like_prefix = f"{rel_dir}/%"
            rows = (await db.scalars(
                select(RepoFileAnalysisState.file_path).where(
                    RepoFileAnalysisState.repo_id == repo_id,
                    RepoFileAnalysisState.file_path.like(like_prefix),
                )
            )).all()
            file_paths = [p for p in rows if os.path.dirname(p) == rel_dir]
        else:
            rows = (await db.scalars(
                select(RepoFileAnalysisState.file_path).where(
                    RepoFileAnalysisState.repo_id == repo_id,
                )
            )).all()
            file_paths = [p for p in rows if "/" not in p]
        
        delete_paths = [p for p in file_paths if p not in existing_files]
        if not delete_paths:
            return
        # 先提交当前会话，避免与 delete_file_analysis_data 的独立会话锁冲突
        await db.commit()
        for rel_file_path in delete_paths:
            try:
                await FileAnalysisService.delete_file_analysis_data(
                    repo_id=repo_id,
                    rel_file_path=rel_file_path,
                    force=True,
                )
            except Exception as e:
                logging.warning(
                    "删除缺失文件分析数据失败 repo_id=%s file_path=%s error=%s",
                    repo_id,
                    rel_file_path,
                    e,
                )

    @staticmethod
    async def _delete_files_under_excluded_dirs(
        repo_id: str,
        excluded_dirs: Set[str],
    ) -> None:
        """删除目录中排除的文件级分析状态记录。
        Args:
            repo_id: 代码仓ID。
            excluded_dirs: 排除的目录集合。
        """
        if not excluded_dirs:
            return
        
        async with get_db_session() as db:
            conds = []
            for p in excluded_dirs:
                conds.append(RepoFileAnalysisState.file_path == p)
                conds.append(RepoFileAnalysisState.file_path.like(f"{p}/%"))
            file_paths = (
                await db.scalars(
                    select(RepoFileAnalysisState.file_path).where(
                        RepoFileAnalysisState.repo_id == repo_id,
                        or_(*conds),
                    )
                )
            ).all()

        for rel_file_path in file_paths:
            try:
                await FileAnalysisService.delete_file_analysis_data(
                    repo_id=repo_id,
                    rel_file_path=rel_file_path,
                    force=True,
                )
            except Exception as e:
                logging.warning("删除 excluded_dirs 下文件分析数据失败 repo_id=%s file_path=%s error=%s", repo_id, rel_file_path, e)

    @staticmethod
    async def _finish_scan_task(
        repo_id: str,
        status: str,
        last_error: Optional[str],
    ) -> None:
        async with get_db_session() as db:
            task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
            if not task:
                return
            task.scan_status = status
            task.last_error = last_error
            task.last_scan_finished_at = datetime.now()
            task.scan_heartbeat_at = datetime.now()
            await db.commit()
        if status == RepoAnalysisStatus.COMPLETED.value:
            try:
                from .nl2code_enhance.lexicon import RepoIdentifierLexicon

                RepoIdentifierLexicon.invalidate_repo(repo_id)
            except Exception as e:
                logging.debug("失效 NL lexicon 缓存失败 repo_id=%s error=%s", repo_id, e)
            try:
                from .scan_change_detector import ScanChangeDetector

                async with get_db_session() as db:
                    repo = await db.scalar(
                        select(GitRepository).where(GitRepository.id == repo_id)
                    )
                if repo and repo.local_path and ScanChangeDetector.is_git_repo(repo.local_path):
                    head = ScanChangeDetector.current_head(repo.local_path)
                    ScanChangeDetector.save_git_head(repo_id, head)
            except Exception as e:
                logging.debug("保存扫描 git fingerprint 失败 repo_id=%s error=%s", repo_id, e)

    @staticmethod
    def _scan_task_to_dict(
        task: RepoAnalysisTask,
        repo_path: Optional[str],
    ) -> Dict[str, object]:
        return {
            "repo_id": task.repo_id,
            "repo_path": repo_path,
            "scan_status": task.scan_status,
            "last_error": task.last_error,
            "last_scan_started_at": task.last_scan_started_at.isoformat() if task.last_scan_started_at else None,
            "last_scan_finished_at": task.last_scan_finished_at.isoformat() if task.last_scan_finished_at else None,
            "scan_heartbeat_at": task.scan_heartbeat_at.isoformat() if task.scan_heartbeat_at else None,
        }

    @staticmethod
    def _resolve_target_type(
        repo_root: str,
        target_rel_path: str,
    ) -> tuple[bool, str]:
        """解析目标路径类型：是否为目录、规范化路径。
        Args:
            repo_root: 仓库根路径。
            target_rel_path: 目标文件或目录路径（相对仓库根）。
        Returns:
            tuple[bool, str]: 是否为目录、规范化路径。
        """
        raw = target_rel_path.strip()
        if not raw:
            raise ValueError("target_rel_path 不能为空")
        
        normalized_slash = normalize_path(raw)
        dir_hint = normalized_slash.endswith("/")
        norm = normalized_slash.strip("/")
        if not norm:
            raise ValueError("target_rel_path 不能为空")
        
        abs_target = os.path.normpath(os.path.join(repo_root, *norm.split("/")))
        if os.path.lexists(abs_target):
            return os.path.isdir(abs_target), norm
        
        if dir_hint:
            return True, norm
        raise ValueError(f"路径在仓库中不存在或无可分析的源码文件: {target_rel_path}")

    @staticmethod
    async def can_query(repo_id: str) -> bool:
        summary = await AnalysisService.get_summary(repo_id)
        return bool(summary.get("searchable"))

    @staticmethod
    async def get_summary(
        repo_id: str,
    ) -> Dict[str, object]:
        async with get_db_session() as db:
            rows = (await db.execute(
                select(
                    RepoFileAnalysisState.chunk_status,
                    RepoFileAnalysisState.summary_status,
                    func.count(RepoFileAnalysisState.id),
                )
                .where(RepoFileAnalysisState.repo_id == repo_id)
                .group_by(
                    RepoFileAnalysisState.chunk_status,
                    RepoFileAnalysisState.summary_status,
                )
            )).all()

            chunk_idle = 0
            chunk_completed = 0
            chunk_running = 0
            chunk_failed = 0
            chunk_skipped = 0
            summary_idle = 0
            summary_completed = 0
            summary_running = 0
            summary_failed = 0
            summary_skipped = 0
            for chunk_st, summary_st, cnt in rows:
                count = int(cnt or 0)
                if chunk_st == FileAnalysisStatus.IDLE.value:
                    chunk_idle += count
                elif chunk_st == FileAnalysisStatus.COMPLETED.value:
                    chunk_completed += count
                elif chunk_st == FileAnalysisStatus.RUNNING.value:
                    chunk_running += count
                elif chunk_st == FileAnalysisStatus.FAILED.value:
                    chunk_failed += count
                elif chunk_st == FileAnalysisStatus.SKIPPED.value:
                    chunk_skipped += count
                if summary_st == FileAnalysisStatus.IDLE.value:
                    summary_idle += count
                elif summary_st == FileAnalysisStatus.COMPLETED.value:
                    summary_completed += count
                elif summary_st == FileAnalysisStatus.RUNNING.value:
                    summary_running += count
                elif summary_st == FileAnalysisStatus.FAILED.value:
                    summary_failed += count
                elif summary_st == FileAnalysisStatus.SKIPPED.value:
                    summary_skipped += count
            total = chunk_idle + chunk_completed + chunk_running + chunk_failed + chunk_skipped

            scan = await AnalysisService.get_scan_status(repo_id)
            in_memory_scan = (
                repo_id in AnalysisService._running_scan_tasks
                and not AnalysisService._running_scan_tasks[repo_id].done()
            )

        finished_raw = scan.get("last_scan_finished_at")
        index_age_seconds: Optional[int] = None
        if finished_raw:
            try:
                finished_at = datetime.fromisoformat(str(finished_raw))
                index_age_seconds = max(
                    0, int((datetime.now() - finished_at).total_seconds())
                )
            except ValueError:
                index_age_seconds = None

        return {
            "total": total,
            "chunk_idle": chunk_idle,
            "chunk_completed": chunk_completed,
            "chunk_running": chunk_running,
            "chunk_failed": chunk_failed,
            "chunk_skipped": chunk_skipped,
            "summary_idle": summary_idle,
            "summary_completed": summary_completed,
            "summary_running": summary_running,
            "summary_failed": summary_failed,
            "summary_skipped": summary_skipped,
            "searchable": chunk_running == 0 and total > 0,
            "in_memory_scan": in_memory_scan,
            "index_age_seconds": index_age_seconds,
        }

    @staticmethod
    async def get_scan_status(
        repo_id: str,
    ) -> Dict[str, object]:
        async with get_db_session() as db:
            task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
            if not task:
                return {
                    "repo_id": repo_id,
                    "scan_status": RepoAnalysisStatus.IDLE.value,
                    "last_error": None,
                    "last_scan_started_at": None,
                    "last_scan_finished_at": None,
                    "scan_heartbeat_at": None,
                }
            return AnalysisService._scan_task_to_dict(task, None)

    @staticmethod
    async def stop_scan(
        repo_id: str,
        reason: str = "scan stopped by user",
    ) -> Dict[str, object]:
        async with get_db_session() as db:
            task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
            if not task:
                return {
                    "repo_id": repo_id,
                    "scan_status": RepoAnalysisStatus.IDLE.value,
                    "last_error": None,
                    "last_scan_started_at": None,
                    "last_scan_finished_at": None,
                    "scan_heartbeat_at": None,
                }

            now = datetime.now()
            task.scan_status = RepoAnalysisStatus.FAILED.value
            task.last_error = reason
            task.last_scan_finished_at = now
            task.scan_heartbeat_at = now
            await db.commit()
            await db.refresh(task)
        
        running_task = AnalysisService._running_scan_tasks.get(repo_id)
        if running_task and not running_task.done():
            running_task.cancel()
            try:
                await asyncio.wait_for(running_task, timeout=5)
            except Exception:
                pass

        return AnalysisService._scan_task_to_dict(task, None)

    @staticmethod
    async def delete_repo_analysis_data(
        repo_id: str,
    ) -> None:
        try:
            await FileAnalysisService.stop_repo_analysis(repo_id)
        except Exception as e:
            logging.warning("停止文件分析 worker 失败 repo_id=%s error=%s", repo_id, e)

        async with get_db_session() as db:
            task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
            if task and task.scan_status == RepoAnalysisStatus.RUNNING.value:
                await AnalysisService.stop_scan(
                    repo_id=repo_id,
                    reason="delete repo analysis data (force stop scan)",
                )

            await db.execute(delete(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
            await db.execute(delete(RepoFileAnalysisState).where(RepoFileAnalysisState.repo_id == repo_id))
            await db.commit()

        try:
            await CodeVectorService.delete_repo_vector_records(repo_id)
        except Exception as e:
            logging.warning("删除 repo 向量数据失败 repo_id=%s error=%s", repo_id, e)

        try:
            from ...lib_analysis.services.api_vector import ApiVectorService

            await ApiVectorService.delete_repo_vector_records(repo_id)
        except Exception as e:
            logging.warning("删除 Lib API 向量数据失败 repo_id=%s error=%s", repo_id, e)

        # 删除 codegraph 中该 repo 的全部数据
        if settings.code_graph_enabled:
            generator = None
            try:
                generator = CodeGraphGateway.create_generator(repo_id, "", "")
                await generator.delete_repo_graph()
            except Exception as e:
                logging.warning("删除 repo codegraph 数据失败 repo_id=%s error=%s", repo_id, e)
            finally:
                if generator:
                    try:
                        generator.close()
                    except Exception:
                        pass
