"""Cron 领域模型：调度规格、任务负载、运行状态、Cron 任务。"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CronKind(str, Enum):
    """Cron 任务到期后的执行类型。"""
    REMIND = "remind"
    AGENT = "agent"


@dataclass
class CronSchedule:
    """调度规格：支持指定时间点(at)、固定间隔(every)、cron 表达式。"""
    kind: str  # "at" | "every" | "cron"
    at_ms: Optional[int] = None
    every_ms: Optional[int] = None
    expr: Optional[str] = None
    tz: Optional[str] = None


@dataclass
class CronPayload:
    """
    任务负载：描述到期后的执行内容。字段与 Agent 一致：user_id、channel_type、channel_id。
    - REMIND: 通知用户。用 message；need_deliver/user_id/channel_type/channel_id 控制推送。
    - AGENT: 调用 Agent。agent_type 指定类型；message 或 extra 可带任务描述。
    - trigger_session_id: 创建/触发该定时任务的会话 ID，便于溯源与回传。
    """
    kind: CronKind
    message: str = ""
    trigger_session_id: Optional[str] = None
    need_deliver: bool = False
    user_id: Optional[str] = None
    channel_type: Optional[str] = None
    channel_id: Optional[str] = None
    agent_type: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CronJobState:
    """任务运行状态。"""
    next_run_at_ms: Optional[int] = None
    last_run_at_ms: Optional[int] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None


@dataclass
class CronJob:
    """Cron 任务。"""
    id: str
    name: str
    enabled: bool
    schedule: CronSchedule
    payload: CronPayload
    state: CronJobState
    created_at_ms: int
    updated_at_ms: int
    delete_after_run: bool = False
    durable: bool = True
