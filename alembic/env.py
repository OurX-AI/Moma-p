from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.config.settings import settings
from app.infrastructure.database import Base
import app.agents.sessions.models
import app.codebase.repo_mgmt.models
import app.codebase.repo_analysis.models.analysis_status
import app.codebase.repo_analysis.models.experience_status

# 不调用 fileConfig(alembic.ini)：它会用 disable_existing_loggers=True 禁用 MOMA 已有 logger，
# 并把 root handler 换成 StreamHandler(sys.stderr)，导致 TUI 模式下 WARNING/ERROR 全跑到 stderr
# 被 textual 显示在界面上。MOMA 的 setup_logging 已统一配置 file/console handler，alembic 日志
# 经 root logger 透传即可。
config = context.config

target_metadata = Base.metadata


def _sync_database_url(url: str) -> str:
    if "+asyncpg" in url:
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if "+aiomysql" in url:
        return url.replace("mysql+aiomysql://", "mysql+pymysql://", 1)
    if "+aiosqlite" in url:
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def run_migrations_offline() -> None:
    url = _sync_database_url(settings.database_url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _sync_database_url(settings.database_url)
    if url.startswith("sqlite"):
        connectable = create_engine(url, poolclass=NullPool)
    else:
        connectable = create_engine(url, pool_pre_ping=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
