from sqlalchemy import Column,DateTime,Boolean,Text,func,String
from app.infrastructure.database import Base


class RepoKind:
    """仓库类型常量。"""
    CODE = "code"
    LIB = "lib"
    VALUES = (CODE, LIB)


class GitRepository(Base):
    """Git仓库模型"""
    __tablename__ = "git_repositories"

    id = Column[str](String, primary_key=True, index=True, comment="ID")
    user_id = Column(String, nullable=False, comment="用户ID")

    git_provider = Column(String, nullable=False, comment="Git提供商")
    repository_url = Column(String, nullable=False, comment="仓库URL")
    organization = Column(String, nullable=False, comment="组织")
    repository_name = Column(String, nullable=False, comment="仓库名称")
    branch = Column(String, default="main", comment="分支")
    description = Column(Text, default="", comment="仓库描述")
    kind = Column(String, nullable=False, default=RepoKind.CODE, comment="类型: code|lib")

    local_path = Column(String, nullable=True, comment="本地路径")

    is_cloned = Column(Boolean, default=False, comment="是否已克隆")
    last_sync_time = Column(DateTime, nullable=True, comment="最后同步时间")

    created_at = Column(DateTime,nullable=False,server_default=func.now())
    updated_at = Column(DateTime,nullable=False,server_default=func.now(),onupdate=func.now())

    def to_dict(self):
        """转换为字典"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "git_provider": self.git_provider,
            "repository_url": self.repository_url,
            "organization": self.organization,
            "repository_name": self.repository_name,
            "branch": self.branch,
            "description": self.description,
            "kind": self.kind,
            "local_path": self.local_path,
            "is_cloned": self.is_cloned,
            "last_sync_time": self.last_sync_time.isoformat() if self.last_sync_time else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def get_name(self):
        """从local_path获取名称"""
        if self.local_path:
            return self.local_path.split('/')[-1] if '/' in self.local_path else self.local_path
        return ""

    def get_full_path(self):
        """获取完整路径"""
        return self.local_path
