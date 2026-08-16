from pathlib import Path

from alembic import command
from alembic.config import Config

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def upgrade_head() -> None:
    """执行 Alembic 迁移至最新版本。"""
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
