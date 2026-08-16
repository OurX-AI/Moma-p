import logging
from typing import Optional
from app.config.settings import APP_NAME, APP_VERSION
from app.infrastructure.database import close_db
from app.infrastructure.database.migrate import upgrade_head
from app.logger import setup_logging

_runtime_inited = False
_schema_migrated = False
_long_session = False


def begin_long_session() -> None:
    global _long_session
    _long_session = True


def end_long_session() -> None:
    global _long_session
    _long_session = False


def is_long_session() -> bool:
    return _long_session


async def init_runtime() -> None:
    """轻量初始化：日志、数据库迁移（所有 CLI 命令共用）。"""
    global _runtime_inited, _schema_migrated
    setup_logging()
    if not _schema_migrated:
        upgrade_head()
        _schema_migrated = True
    if not _runtime_inited:
        _runtime_inited = True
        logging.info("%s v%s 运行环境已就绪", APP_NAME, APP_VERSION)


async def ensure_scheduler(repo_path: Optional[str] = None) -> None:
    """启动文件分析调度器；可选启动已登记仓库的增量扫描。

    repo_path 非空时增量扫描只扫该仓（TUI 模式）；为空时扫所有已登记仓（常驻服务模式）。
    """
    from .repo_analysis.services.file_analysis_service import FileAnalysisService
    from .repo_analysis.services.incremental_scan_service import IncrementalScanService

    FileAnalysisService.start_global_scheduler()
    IncrementalScanService.start(repo_path=repo_path)

async def startup() -> None:
    await init_runtime()
    await ensure_scheduler()


async def release_runtime() -> None:
    """释放 DB 连接（短命令退出时用；REPL 长会话中不调用）。"""
    await close_db()


async def shutdown() -> None:
    global _runtime_inited
    end_long_session()
    from .repo_analysis.services.file_analysis_service import FileAnalysisService
    from .repo_analysis.services.incremental_scan_service import IncrementalScanService
    from .repo_analysis.services.lsp.lsp_service import CodeLSPService
    from app.infrastructure.vector_store import get_vector_store_conn

    await IncrementalScanService.stop()
    await FileAnalysisService.stop_global_scheduler()
    await CodeLSPService.close_all()
    conn = get_vector_store_conn()
    if conn and hasattr(conn, "close"):
        try:
            await conn.close()
        except Exception as e:
            logging.warning("关闭向量存储连接时出错: %s", e)
    await close_db()
    _runtime_inited = False
