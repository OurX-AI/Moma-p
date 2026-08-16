from __future__ import annotations
import asyncio
from enum import Enum
from typing import Optional


class AbortReason(str, Enum):
    """Run 级中止原因；首个触发的 reason 会被记录，后续 request_abort 不覆盖。"""
    USER_INTERRUPT = "user_interrupt"   # 用户中断
    SUPERSEDED = "superseded" # 被新消息取代
    MAX_STEPS = "max_steps" # 最大步数
    RUNTIME_ERROR = "runtime_error" # 运行时错误
    TASK_CANCELLED = "task_cancelled" # 任务取消


_ABORT_REASON_LABELS: dict[AbortReason, str] = {
    AbortReason.USER_INTERRUPT: "user interrupt",
    AbortReason.SUPERSEDED: "superseded by a new message",
    AbortReason.MAX_STEPS: "max steps reached",
    AbortReason.RUNTIME_ERROR: "runtime error",
    AbortReason.TASK_CANCELLED: "task cancelled",
}


class RunAbortController:
    """Agent 级 Abort 控制器；每轮 run 通过 clear() 复位，实例在 Agent.__init__ 创建并复用。

    使用 asyncio.Event 作为协作式信号：Agent 循环、Factory、长工具在检查点轮询 is_aborted()。
    不抛异常穿透调用栈，避免与 Tool 正常返回路径混杂。
    """

    def __init__(self) -> None:
        self._abort_event = asyncio.Event()
        self.reason: Optional[AbortReason] = None
        self.message: Optional[str] = None

    def clear(self) -> None:
        """复位本轮 abort 状态（run 结束 / reset / 新 run 开始前调用）。"""
        self._abort_event = asyncio.Event()
        self.reason = None
        self.message = None

    @property
    def signal(self) -> asyncio.Event:
        """Tool 可 await event.wait() 或轮询 is_set()。"""
        return self._abort_event

    def request_abort(self, reason: AbortReason, message: Optional[str] = None) -> None:
        """幂等：多次调用只保留第一次 reason，但始终 set event。"""
        if self.reason is None:
            self.reason = reason
            self.message = message
        self._abort_event.set()

    def is_aborted(self) -> bool:
        return self._abort_event.is_set()

    def reason_label(self) -> str:
        """供用户可见文案使用。"""
        if self.reason is None:
            return "aborted"
        return _ABORT_REASON_LABELS.get(self.reason, self.reason.value)
