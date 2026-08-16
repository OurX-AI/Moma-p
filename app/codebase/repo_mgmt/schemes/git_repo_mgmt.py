from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, validator


class CreateRepositoryFromUrl(BaseModel):
    """通过Git URL创建仓库"""
    repository_url: str = Field(..., description="Git仓库URL")
    branch: str = Field(default="main", description="分支名称")
    description: str = Field(default="", description="仓库描述")
    
    @validator('repository_url')
    def validate_repository_url(cls, v):
        if not v.strip():
            raise ValueError("仓库URL不能为空")
        if not v.startswith(('http://', 'https://', 'git@')):
            raise ValueError("无效的Git仓库URL")
        return v.strip()
    
    @validator('branch')
    def validate_branch(cls, v):
        if not v.strip():
            raise ValueError("分支名称不能为空")
        return v.strip()


class UpdateRepository(BaseModel):
    """更新仓库"""
    description: Optional[str] = Field(None, description="仓库描述")
    branch: Optional[str] = Field(None, description="分支名称")
    repository_url: Optional[str] = Field(None, description="Git远端URL")
    
    @validator('branch')
    def validate_branch(cls, v):
        if v is not None and not v.strip():
            raise ValueError("分支名称不能为空")
        return v.strip() if v else v

    @validator('repository_url')
    def validate_repository_url(cls,v):
        if v is None:
            return v
        v=v.strip()
        if not v:
            raise ValueError("repository_url不能为空")
        return v


class RepositoryInfo(BaseModel):
    """仓库信息"""
    id: str = Field(..., description="仓库ID")
    user_id: str = Field(..., description="用户ID")
    
    # 基本信息
    git_provider: str = Field(..., description="Git提供商")
    repository_url: str = Field(..., description="仓库URL")
    organization: str = Field(..., description="组织")
    repository_name: str = Field(..., description="仓库名称")
    description: str = Field(..., description="仓库描述")
    kind: str = Field(default="code", description="类型: code|lib")
    branch: str = Field(..., description="分支")
    
    # 路径信息
    local_path: Optional[str] = Field(None, description="本地路径")
    
    # 状态信息
    is_cloned: bool = Field(..., description="是否已克隆")
    last_sync_time: Optional[datetime] = Field(None, description="最后同步时间")
    
    # 时间信息
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
    
    class Config:
        from_attributes = True 
