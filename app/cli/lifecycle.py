import logging
from app.config.settings import settings, APP_NAME, APP_VERSION
from app.infrastructure.database import Base, close_db, get_db_session
from app.agents.mcp.pool import MCP_POOL
from app.agents.tools.exec.process_manager import PROCESS_MANAGER
from app.agents.tools.web.browser_session import BROWSER_SESSION_MANAGER
from app.services.cron import start_cron, stop_cron
from app.logger import setup_logging


async def _ensure_workspace_registered(
    workspace_path: str,
    user_id: str,
) -> None:
    """一次性登记当前 workspace（不启动定时器）。"""
    from app.codebase.integration.orchestrator import AutoAnalyzeOrchestrator

    if not workspace_path.strip():
        return
    try:
        result = await AutoAnalyzeOrchestrator.ensure_workspace_registered(
            workspace_path=workspace_path.strip(),
            user_id=(user_id or "cli").strip() or "cli",
        )
        logging.info(
            "CodeBase workspace 已登记: repo_id=%s path=%s status=%s",
            result.repo_id,
            result.repo_path,
            result.status,
        )
    except Exception as exc:
        logging.warning("CodeBase workspace 登记失败: %s", exc)


async def startup(
    *,
    quiet_console: bool = False,
    workspace_path: str = "",
    user_id: str = "",
) -> None:
    setup_logging(console=not quiet_console)
    logging.info("启动 %s v%s (CLI 模式)", APP_NAME, APP_VERSION)
    # 先跑 Alembic（含 CodeBase 表/缺列），再 create_all 兜底新建表
    try:
        import app.agents.sessions.models  # noqa: F401
        import app.codebase.repo_mgmt.models  # noqa: F401
        import app.codebase.repo_analysis.models.analysis_status  # noqa: F401
        import app.codebase.repo_analysis.models.experience_status  # noqa: F401
        from app.infrastructure.database.migrate import upgrade_head
        upgrade_head()
        # alembic 迁移可能改动 root logger handler，重新恢复 MOMA 的 file/console 配置
        setup_logging(console=not quiet_console)
        logging.info("数据库迁移 upgrade_head 完成")
    except Exception as exc:
        logging.warning("数据库迁移失败，将尝试 create_all 兜底: %s", exc)
    if settings.database_type.lower() == "sqlite" and not settings.enable_local_session_storage:
        async with get_db_session() as session:
            conn = await session.connection()
            await conn.run_sync(Base.metadata.create_all)
        logging.info("SQLite 表结构检查完成")
    # CodeBase：一次性登记当前 workspace，然后启动增量扫描调度器
    await _ensure_workspace_registered(workspace_path, user_id)
    try:
        from app.codebase.runtime import ensure_scheduler
        await ensure_scheduler(repo_path=workspace_path.strip() or None)
        logging.info("CodeBase 文件分析调度器已启动 workspace=%s", workspace_path.strip() or "(none)")
    except Exception as exc:
        logging.warning("CodeBase 文件分析调度器启动失败: %s", exc)
    MCP_POOL.start_idle_cleanup()
    PROCESS_MANAGER.start_prune_loop()
    if settings.enable_cron:
        start_cron()
        logging.info("Cron 调度循环已启动")
    else:
        logging.info("Cron 调度循环未启动（ENABLE_CRON=false）")


async def shutdown() -> None:
    MCP_POOL.stop_idle_cleanup()
    await PROCESS_MANAGER.shutdown()
    await BROWSER_SESSION_MANAGER.shutdown()
    try:
        from app.codebase.runtime import shutdown as codebase_shutdown
        await codebase_shutdown()
    except Exception as exc:
        logging.warning("停止 CodeBase 运行时出错: %s", exc)
    if settings.enable_cron:
        try:
            await stop_cron()
        except Exception as exc:
            logging.warning("停止 Cron 调度时出错: %s", exc)
    try:
        await close_db()
    except Exception as exc:
        logging.warning("关闭数据库连接时出错: %s", exc)
    logging.info("CLI 已退出")
