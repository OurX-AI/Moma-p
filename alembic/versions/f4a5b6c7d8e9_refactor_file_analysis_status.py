"""refactor repo_file_analysis_state: split status into chunk_status + summary_status

Revision ID: f4a5b6c7d8e9
Revises: e0f1g2h3i4j5
Create Date: 2026-08-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, None] = "e0f1g2h3i4j5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "repo_file_analysis_state"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in _inspector().get_columns(table))


def upgrade() -> None:
    if not _has_table(TABLE):
        return

    # --- 第一步：添加新列 ---
    if not _has_column(TABLE, "chunk_status"):
        op.add_column(TABLE, sa.Column(
            "chunk_status", sa.String(32), nullable=False,
            server_default="idle",
            comment="行块向量化状态: idle|running|completed|failed|skipped",
        ))
    if not _has_column(TABLE, "summary_status"):
        op.add_column(TABLE, sa.Column(
            "summary_status", sa.String(32), nullable=False,
            server_default="idle",
            comment="符号摘要分析状态: idle|running|completed|failed|skipped",
        ))
    if not _has_column(TABLE, "last_chunk_started_at"):
        op.add_column(TABLE, sa.Column("last_chunk_started_at", sa.DateTime(), nullable=True))
    if not _has_column(TABLE, "last_chunk_finished_at"):
        op.add_column(TABLE, sa.Column("last_chunk_finished_at", sa.DateTime(), nullable=True))
    if not _has_column(TABLE, "last_summary_started_at"):
        op.add_column(TABLE, sa.Column("last_summary_started_at", sa.DateTime(), nullable=True))
    if not _has_column(TABLE, "last_summary_finished_at"):
        op.add_column(TABLE, sa.Column("last_summary_finished_at", sa.DateTime(), nullable=True))

    # --- 第二步：迁移数据 ---
    # CODE 仓库：is_embedded → chunk_status, is_symboled → summary_status
    # LIB 仓库：不做 embedding，is_embedded → summary_status（API 摘要结果）
    op.execute(f"""
        UPDATE {TABLE}
        SET chunk_status = CASE
            WHEN repo_id IN (SELECT id FROM git_repositories WHERE kind = 'lib') THEN 'idle'
            WHEN is_embedded = 1 THEN 'completed'
            WHEN status = 'pending' THEN 'idle'
            ELSE status
        END,
        summary_status = CASE
            WHEN repo_id IN (SELECT id FROM git_repositories WHERE kind = 'lib') THEN
                CASE WHEN is_embedded = 1 THEN 'completed'
                     WHEN status = 'pending' THEN 'idle'
                     ELSE status END
            WHEN is_symboled = 1 THEN 'completed'
            WHEN status = 'pending' THEN 'idle'
            ELSE status
        END,
        last_chunk_started_at = CASE
            WHEN repo_id IN (SELECT id FROM git_repositories WHERE kind = 'lib') THEN NULL
            WHEN is_embedded = 1 THEN last_started_at
            ELSE NULL
        END,
        last_chunk_finished_at = CASE
            WHEN repo_id IN (SELECT id FROM git_repositories WHERE kind = 'lib') THEN NULL
            WHEN is_embedded = 1 THEN last_finished_at
            ELSE NULL
        END,
        last_summary_started_at = CASE
            WHEN repo_id IN (SELECT id FROM git_repositories WHERE kind = 'lib') THEN
                CASE WHEN is_embedded = 1 THEN last_started_at ELSE NULL END
            WHEN is_symboled = 1 THEN last_started_at
            ELSE NULL
        END,
        last_summary_finished_at = CASE
            WHEN repo_id IN (SELECT id FROM git_repositories WHERE kind = 'lib') THEN
                CASE WHEN is_embedded = 1 THEN last_finished_at ELSE NULL END
            WHEN is_symboled = 1 THEN last_finished_at
            ELSE NULL
        END
    """)

    # --- 第三步：添加新索引 ---
    op.create_index("idx_repo_file_analysis_chunk", TABLE, ["repo_id", "chunk_status"])
    op.create_index("idx_repo_file_analysis_summary", TABLE, ["repo_id", "summary_status"])

    # --- 第四步：删除旧索引和旧列 ---
    try:
        op.drop_index("idx_repo_file_analysis_dispatch", table_name=TABLE)
    except Exception:
        pass

    for col in ["status", "is_embedded", "is_symboled", "last_started_at", "last_finished_at"]:
        if _has_column(TABLE, col):
            op.drop_column(TABLE, col)


def downgrade() -> None:
    if not _has_table(TABLE):
        return

    # --- 回退：添加旧列 ---
    for col, default in [
        ("status", "idle"),
        ("is_embedded", "0"),
        ("is_symboled", "0"),
    ]:
        if not _has_column(TABLE, col):
            nullable = col == "status"
            op.add_column(TABLE, sa.Column(
                col,
                sa.String(32) if col == "status" else sa.Boolean(),
                nullable=nullable,
                server_default=default,
            ))

    if not _has_column(TABLE, "last_started_at"):
        op.add_column(TABLE, sa.Column("last_started_at", sa.DateTime(), nullable=True))
    if not _has_column(TABLE, "last_finished_at"):
        op.add_column(TABLE, sa.Column("last_finished_at", sa.DateTime(), nullable=True))

    # --- 回退：还原数据 ---
    op.execute(f"""
        UPDATE {TABLE}
        SET status = CASE
            WHEN chunk_status = 'completed' AND summary_status = 'completed' THEN 'completed'
            WHEN chunk_status = 'failed' OR summary_status = 'failed' THEN 'failed'
            WHEN chunk_status = 'running' OR summary_status = 'running' THEN 'running'
            ELSE 'idle'
        END,
        is_embedded = CASE WHEN chunk_status = 'completed' THEN 1 ELSE 0 END,
        is_symboled = CASE WHEN summary_status = 'completed' THEN 1 ELSE 0 END,
        last_started_at = COALESCE(last_chunk_started_at, last_summary_started_at),
        last_finished_at = COALESCE(last_chunk_finished_at, last_summary_finished_at)
    """)

    # --- 回退：删除新索引和新列 ---
    for idx in ["idx_repo_file_analysis_chunk", "idx_repo_file_analysis_summary"]:
        try:
            op.drop_index(idx, table_name=TABLE)
        except Exception:
            pass

    for col in [
        "chunk_status", "summary_status",
        "last_chunk_started_at", "last_chunk_finished_at",
        "last_summary_started_at", "last_summary_finished_at",
    ]:
        if _has_column(TABLE, col):
            op.drop_column(TABLE, col)

    # --- 回退：重建旧索引 ---
    op.create_index("idx_repo_file_analysis_dispatch", TABLE, ["repo_id", "status"])
