"""add codebase analysis tables and columns

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in _inspector().get_columns(table))


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _has_table(table):
        return
    if _has_column(table, column.name):
        return
    op.add_column(table, column)


def upgrade() -> None:
    # --- git_repositories ---
    if not _has_table("git_repositories"):
        op.create_table(
            "git_repositories",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("git_provider", sa.String(), nullable=False),
            sa.Column("repository_url", sa.String(), nullable=False),
            sa.Column("organization", sa.String(), nullable=False),
            sa.Column("repository_name", sa.String(), nullable=False),
            sa.Column("branch", sa.String(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("kind", sa.String(), nullable=False, server_default="code"),
            sa.Column("local_path", sa.String(), nullable=True),
            sa.Column("is_cloned", sa.Boolean(), nullable=True, server_default=sa.text("0")),
            sa.Column("last_sync_time", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_git_repositories_id", "git_repositories", ["id"])
    else:
        _add_column_if_missing(
            "git_repositories",
            sa.Column("kind", sa.String(), nullable=False, server_default="code", comment="类型: code|lib"),
        )

    # --- git_authorities ---
    if not _has_table("git_authorities"):
        op.create_table(
            "git_authorities",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("access_token", sa.String(length=500), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=True, server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_git_authorities_user_id", "git_authorities", ["user_id"])
        op.create_index("idx_user_provider", "git_authorities", ["user_id", "provider"])

    # --- repo_analysis_tasks ---
    if not _has_table("repo_analysis_tasks"):
        op.create_table(
            "repo_analysis_tasks",
            sa.Column("repo_id", sa.String(length=36), sa.ForeignKey("git_repositories.id"), primary_key=True),
            sa.Column("scan_status", sa.String(length=32), nullable=False, server_default="idle"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_scan_started_at", sa.DateTime(), nullable=True),
            sa.Column("last_scan_finished_at", sa.DateTime(), nullable=True),
            sa.Column("scan_heartbeat_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("idx_repo_analysis_task_scan_status", "repo_analysis_tasks", ["scan_status"])

    # --- repo_file_analysis_state ---
    if not _has_table("repo_file_analysis_state"):
        op.create_table(
            "repo_file_analysis_state",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("repo_id", sa.String(length=36), sa.ForeignKey("git_repositories.id"), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("is_embedded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("is_symboled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_started_at", sa.DateTime(), nullable=True),
            sa.Column("last_finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("repo_id", "file_path", name="uq_repo_file"),
        )
        op.create_index("idx_repo_file_analysis_lookup", "repo_file_analysis_state", ["repo_id", "file_path"])
        op.create_index("idx_repo_file_analysis_dispatch", "repo_file_analysis_state", ["repo_id", "status"])
    else:
        _add_column_if_missing(
            "repo_file_analysis_state",
            sa.Column("is_embedded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )
        _add_column_if_missing(
            "repo_file_analysis_state",
            sa.Column("is_symboled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )

    # --- repo_experience_tasks ---
    if not _has_table("repo_experience_tasks"):
        op.create_table(
            "repo_experience_tasks",
            sa.Column("repo_id", sa.String(length=36), sa.ForeignKey("git_repositories.id"), primary_key=True),
            sa.Column("job_status", sa.String(length=32), nullable=False, server_default="idle"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_started_at", sa.DateTime(), nullable=True),
            sa.Column("last_finished_at", sa.DateTime(), nullable=True),
            sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ready_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("idx_repo_experience_task_status", "repo_experience_tasks", ["job_status"])

    # --- mr_experience_items ---
    if not _has_table("mr_experience_items"):
        op.create_table(
            "mr_experience_items",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("repo_id", sa.String(length=36), sa.ForeignKey("git_repositories.id"), nullable=False),
            sa.Column("commit_sha", sa.String(length=64), nullable=False),
            sa.Column("commit_message", sa.Text(), nullable=False, server_default=""),
            sa.Column("committed_at", sa.DateTime(), nullable=True),
            sa.Column("is_merge", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidate_files_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("steps_json", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_started_at", sa.DateTime(), nullable=True),
            sa.Column("last_finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("repo_id", "commit_sha", name="uq_mr_experience_repo_commit"),
        )
        op.create_index("idx_mr_experience_dispatch", "mr_experience_items", ["repo_id", "status"])


def downgrade() -> None:
    if _has_table("mr_experience_items"):
        op.drop_table("mr_experience_items")
    if _has_table("repo_experience_tasks"):
        op.drop_table("repo_experience_tasks")
    # 不主动 drop 已有分析表，避免误删生产索引数据；仅回退本迁移新增列时保留空实现。
