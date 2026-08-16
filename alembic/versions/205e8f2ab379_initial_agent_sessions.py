"""initial_agent_sessions

Revision ID: 205e8f2ab379
Revises: 
Create Date: 2026-04-15 08:41:10.519293

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '205e8f2ab379'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # 兼容历史库：部分环境曾用 create_all 建表且 alembic_version 为空
    if _has_table("agent_sessions"):
        return
    op.create_table('agent_sessions',
    sa.Column('session_id', sa.String(length=128), nullable=False, comment='会话ID'),
    sa.Column('description', sa.Text(), nullable=True, comment='会话描述'),
    sa.Column('agent_type', sa.String(length=64), nullable=False, comment='Agent 类型'),
    sa.Column('channel_type', sa.String(length=64), server_default='', nullable=False, comment='渠道类型'),
    sa.Column('user_id', sa.String(length=128), nullable=False, comment='用户ID'),
    sa.Column('llm_provider', sa.String(length=128), server_default='', nullable=False, comment='模型提供者'),
    sa.Column('llm_model', sa.String(length=128), server_default='default', nullable=False, comment='模型名称'),
    sa.Column('messages', sa.JSON(), nullable=False, comment='消息列表 JSON'),
    sa.Column('compaction', sa.JSON(), nullable=True, comment='会话压缩摘要（Message JSON）'),
    sa.Column('last_compacted', sa.Integer(), server_default='0', nullable=False, comment='已压缩消息数'),
    sa.Column('last_consolidated', sa.Integer(), server_default='0', nullable=False, comment='已合并消息数'),
    sa.Column('metadata', sa.JSON(), nullable=True, comment='元数据 JSON'),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='创建时间'),
    sa.Column('last_updated', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='最后更新时间'),
    sa.PrimaryKeyConstraint('session_id')
    )


def downgrade() -> None:
    if _has_table("agent_sessions"):
        op.drop_table('agent_sessions')
