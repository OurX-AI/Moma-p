from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from .message import Message


class SessionCreate(BaseModel):
    """会话创建请求"""
    user_id: str = Field(..., description="用户ID")
    agent_type: Optional[str] = Field(None, description="Agent 类型")
    description: Optional[str] = Field(None, description="会话描述")
    workspace_path: Optional[str] = Field(None, description="用户工作目录，空字符串表示默认沙箱")
    metadata: Optional[Dict[str, Any]] = Field(None, description="会话元数据（plan_graph 只读，由服务端 Planning Agent 管理）")
    llm_provider: Optional[str] = Field(None, description="模型提供者")
    llm_model: Optional[str] = Field(None, description="使用的模型名称")


class SessionUpdateRequest(BaseModel):
    """会话配置更新：Agent 类型、模型信息或元数据，仅传需要修改的字段。"""
    agent_type: Optional[str] = Field(None, description="Agent 类型")
    llm_provider: Optional[str] = Field(None, description="模型提供者")
    llm_model: Optional[str] = Field(None, description="使用的模型名称")
    workspace_path: Optional[str] = Field(None, description="用户工作目录，空字符串表示默认沙箱")
    metadata: Optional[Dict[str, Any]] = Field(None, description="会话元数据，与现有 metadata 合并（plan_graph 只读，客户端不可写入）")


class SessionInfo(BaseModel):
    """会话列表/概要信息"""
    session_id: str = Field(..., description="会话ID")
    user_id: str = Field(..., description="用户ID")
    agent_type: Optional[str] = Field(None, description="Agent 类型")
    channel_type: Optional[str] = Field(None, description="渠道类型")
    created_at: datetime = Field(..., description="创建时间")
    last_updated: datetime = Field(..., description="最后更新时间")
    description: Optional[str] = Field(None, description="会话描述")
    llm_provider: Optional[str] = Field(None, description="模型提供者")
    llm_model: Optional[str] = Field(None, description="模型名称")
    workspace_path: Optional[str] = Field(None, description="用户工作目录")
    metadata: Optional[Dict[str, Any]] = Field(None, description="元数据（服务端内部状态）")

    class Config:
        from_attributes = True


class UserMessage(BaseModel):
    """API 返回的单条消息(用户可读格式)；工具类消息的 content 为 JSON 字符串。"""
    role: str = Field(..., description="角色")
    content: str = Field(..., description="内容")
    create_time: str = Field(..., description="创建时间")