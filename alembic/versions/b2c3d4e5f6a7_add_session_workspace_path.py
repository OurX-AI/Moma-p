"""add session workspace_path column

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if _has_column("agent_sessions", "workspace_path"):
        return
    op.add_column(
        "agent_sessions",
        sa.Column(
            "workspace_path",
            sa.String(length=1024),
            nullable=True,
            comment="用户工作目录",
        ),
    )


def downgrade() -> None:
    if _has_column("agent_sessions", "workspace_path"):
        op.drop_column("agent_sessions", "workspace_path")
