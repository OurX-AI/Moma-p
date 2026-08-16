from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from enum import Enum

class GitAuthProvider(Enum):
    GITHUB = "github"
    GITEE = "gitee"
    GITLAB = "gitlab"

    @classmethod
    def values(cls):
        return [m.value for m in cls]

class GitAuthResponse(BaseModel):
    id: str
    user_id: str
    provider: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True

class GitAuthListResponse(BaseModel):
    items: list[GitAuthResponse]
    total: int
