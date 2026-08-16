"""add last_collected_commit_sha and last_collected_committed_at to repo_experience_task

Revision ID: e0f1g2h3i4j5
Revises: c3d4e5f6a7b8
Create Date: 2026-08-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e0f1g2h3i4j5"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """添加 MR 经验收集水位字段到 repo_experience_tasks 表"""
    op.add_column(
        'repo_experience_tasks',
        sa.Column('last_collected_commit_sha', sa.String(64), nullable=True, comment='最后一次收集到的 MR commit SHA（增量检测用）')
    )
    op.add_column(
        'repo_experience_tasks',
        sa.Column('last_collected_committed_at', sa.DateTime, nullable=True, comment='最后一次收集到的 MR 提交时间（观测用）')
    )


def downgrade() -> None:
    """删除 MR 经验收集水位字段"""
    op.drop_column('repo_experience_tasks', 'last_collected_committed_at')
    op.drop_column('repo_experience_tasks', 'last_collected_commit_sha')
