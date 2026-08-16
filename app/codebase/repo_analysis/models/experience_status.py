import enum
import uuid
from sqlalchemy import Column, String, Text, ForeignKey, Index, DateTime, Integer, UniqueConstraint, func
from app.infrastructure.database import Base


class ExperienceJobStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExperienceItemStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    SKIPPED = "skipped"
    FAILED = "failed"


class RepoExperienceTask(Base):
    """仓级经验分析任务状态。"""

    __tablename__ = "repo_experience_tasks"

    repo_id = Column(String(36), ForeignKey("git_repositories.id"), primary_key=True, comment="代码仓ID")
    job_status = Column(String(32), nullable=False, default=ExperienceJobStatus.IDLE.value, comment="任务状态")
    last_error = Column(Text, nullable=True, comment="最近错误")
    last_started_at = Column(DateTime, nullable=True, comment="最近开始时间")
    last_finished_at = Column(DateTime, nullable=True, comment="最近结束时间")
    total_items = Column(Integer, nullable=False, default=0, comment="条目总数")
    ready_items = Column(Integer, nullable=False, default=0, comment="已就绪条目数")
    failed_items = Column(Integer, nullable=False, default=0, comment="失败条目数")
    skipped_items = Column(Integer, nullable=False, default=0, comment="跳过条目数")
    last_collected_commit_sha = Column(String(64), nullable=True, comment="最后一次收集到的 MR commit SHA（增量检测用）")
    last_collected_committed_at = Column(DateTime, nullable=True, comment="最后一次收集到的 MR 提交时间（观测用）")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("idx_repo_experience_task_status", "job_status"),)


class MrExperienceItem(Base):
    """单条 git 合入/提交对应的经验沉淀条目。"""

    __tablename__ = "mr_experience_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), comment="ID")
    repo_id = Column(String(36), ForeignKey("git_repositories.id"), nullable=False, comment="代码仓ID")
    commit_sha = Column(String(64), nullable=False, comment="commit SHA")
    commit_message = Column(Text, nullable=False, default="", comment="commit message")
    committed_at = Column(DateTime, nullable=True, comment="提交时间")
    is_merge = Column(Integer, nullable=False, default=0, comment="是否 merge commit")
    candidate_files_json = Column(Text, nullable=False, default="[]", comment="规则筛选候选文件 JSON")
    title = Column(Text, nullable=True, comment="LLM 经验标题")
    steps_json = Column(Text, nullable=True, comment="LLM 步骤 JSON")
    status = Column(
        String(32),
        nullable=False,
        default=ExperienceItemStatus.PENDING.value,
        comment="pending|running|ready|skipped|failed",
    )
    last_error = Column(Text, nullable=True, comment="最近错误")
    retry_count = Column(Integer, nullable=False, default=0, comment="重试次数")
    last_started_at = Column(DateTime, nullable=True)
    last_finished_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("repo_id", "commit_sha", name="uq_mr_experience_repo_commit"),
        Index("idx_mr_experience_dispatch", "repo_id", "status"),
    )
