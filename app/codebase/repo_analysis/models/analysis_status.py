import enum
import uuid
from sqlalchemy import Column, String, Text, ForeignKey, Index, DateTime, func, UniqueConstraint, and_, or_
from app.infrastructure.database import Base


class RepoAnalysisStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class FileAnalysisStatus(str, enum.Enum):
    """文件级单项分析状态：chunk_status / summary_status 共用。"""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RepoAnalysisType(str, enum.Enum):
    LINE_CHUNK_VECTOR = "line_chunk_vector"
    SYMBOL_SUMMARY_VECTOR = "symbol_summary_vector"


class RepoAnalysisTask(Base):
    """代码仓级扫描任务状态与分析汇总快照。"""
    __tablename__ = "repo_analysis_tasks"

    repo_id = Column(String(36), ForeignKey("git_repositories.id"), primary_key=True, comment="代码仓ID")
    scan_status = Column(String(32), nullable=False, default=RepoAnalysisStatus.IDLE.value, comment="扫描任务状态")
    last_error = Column(Text, nullable=True, comment="最近错误")
    last_scan_started_at = Column(DateTime, nullable=True, comment="最近扫描开始时间")
    last_scan_finished_at = Column(DateTime, nullable=True, comment="最近扫描结束时间")
    scan_heartbeat_at = Column(DateTime, nullable=True, comment="扫描任务心跳时间")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_repo_analysis_task_scan_status", "scan_status"),
    )


class RepoFileAnalysisState(Base):
    """文件级分析状态：chunk_status / summary_status 各自独立跟踪。"""
    __tablename__ = "repo_file_analysis_state"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), comment="ID")
    repo_id = Column(String(36), ForeignKey("git_repositories.id"), nullable=False, comment="代码仓ID")
    file_path = Column(String(500), nullable=False, comment="相对路径")

    chunk_status = Column(
        String(32),
        nullable=False,
        default=FileAnalysisStatus.IDLE.value,
        comment="行块向量化状态: idle|running|completed|failed|skipped",
    )
    summary_status = Column(
        String(32),
        nullable=False,
        default=FileAnalysisStatus.IDLE.value,
        comment="符号摘要分析状态: idle|running|completed|failed|skipped",
    )

    last_error = Column(Text, nullable=True, comment="最近错误")
    last_chunk_started_at = Column(DateTime, nullable=True, comment="最近向量化开始时间")
    last_chunk_finished_at = Column(DateTime, nullable=True, comment="最近向量化结束时间")
    last_summary_started_at = Column(DateTime, nullable=True, comment="最近摘要分析开始时间")
    last_summary_finished_at = Column(DateTime, nullable=True, comment="最近摘要分析结束时间")

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    @property
    def is_chunk_done(self) -> bool:
        return self.chunk_status == FileAnalysisStatus.COMPLETED.value

    @property
    def is_summary_done(self) -> bool:
        return self.summary_status == FileAnalysisStatus.COMPLETED.value

    @property
    def needs_chunk(self) -> bool:
        return self.chunk_status in (
            FileAnalysisStatus.IDLE.value,
            FileAnalysisStatus.FAILED.value,
        )

    @property
    def needs_summary(self) -> bool:
        return self.summary_status in (
            FileAnalysisStatus.IDLE.value,
            FileAnalysisStatus.FAILED.value,
        )

    @property
    def is_fully_completed(self) -> bool:
        return self.is_chunk_done and self.is_summary_done

    @classmethod
    def needs_analysis_filter(cls, chunk_on: bool = True, summary_on: bool = True):
        """返回需要分析的过滤条件：根据开关状态检查对应阶段。

        为了避免竞态条件（多个 worker 同时处理同一个文件）：
        - 两个阶段都不能是 running
        - 至少有一个阶段需要分析（idle 或 failed）
        """
        _claimable = [FileAnalysisStatus.IDLE.value, FileAnalysisStatus.FAILED.value]
        _running = [FileAnalysisStatus.RUNNING.value]

        # 条件1：两个阶段都不能是 running
        not_running_conditions = [
            ~cls.chunk_status.in_(_running),
            ~cls.summary_status.in_(_running),
        ]

        # 条件2：至少有一个阶段需要分析
        claimable_conditions = []
        if chunk_on:
            claimable_conditions.append(cls.chunk_status.in_(_claimable))
        if summary_on:
            claimable_conditions.append(cls.summary_status.in_(_claimable))

        if not claimable_conditions:
            return False

        # 最终条件：(两个都不是 running) AND (至少一个需要分析)
        return and_(
            and_(*not_running_conditions),
            or_(*claimable_conditions) if len(claimable_conditions) > 1 else claimable_conditions[0]
        )

    __table_args__ = (
        UniqueConstraint("repo_id", "file_path", name="uq_repo_file"),
        Index("idx_repo_file_analysis_lookup", "repo_id", "file_path"),
        Index("idx_repo_file_analysis_chunk", "repo_id", "chunk_status"),
        Index("idx_repo_file_analysis_summary", "repo_id", "summary_status"),
    )
