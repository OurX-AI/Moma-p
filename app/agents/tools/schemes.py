from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from ..sessions.message import ToolCall


@dataclass
class ToolCallItem:
    tool_call_id: str
    tool_name: str
    tool_params: Dict[str, Any]

    @classmethod
    def from_tool_call(cls, tool_call: "ToolCall") -> "ToolCallItem":
        return cls(
            tool_call_id=tool_call.id,
            tool_name=tool_call.function.name,
            tool_params=dict(tool_call.function.arguments or {}),
        )


class ToolResultStatus(Enum):
    """工具执行结果状态枚举"""
    TOOL_NOT_FOUND = "tool_not_found"  # 工具不存在
    EXECUTE_SUCCESS = "execute_success"  # 执行成功
    EXECUTE_TIMEOUT = "execute_timeout"  # 执行超时
    EXECUTE_ERROR = "execute_error"  # 执行错误
    EXECUTE_CANCELLED = "execute_cancelled"  # 执行取消
    EXECUTE_ABORTED = "execute_aborted"  # 执行中止
    EXECUTE_INTERRUPTED = "execute_interrupted"  # 执行中断
    EXECUTE_PAUSED = "execute_paused"  # 执行暂停
    EXECUTE_RESUMED = "execute_resumed"  # 执行恢复

class ToolResult:
    """工具执行结果。可选 metadata 供工具自行标注 truncated/outputPath 等。"""
    def __init__(self, status: ToolResultStatus, result: Any, metadata: Optional[Dict[str, Any]] = None):
        self.status = status
        self.result = result
        self.metadata = metadata

    def __bool__(self):
        """判断工具执行结果是否成功"""
        return self.status == ToolResultStatus.EXECUTE_SUCCESS

    def to_json(self) -> Dict:
        """Convert tool result to JSON format."""
        out = {"status": self.status.value, "result": self.result}
        if self.metadata is not None:
            out["metadata"] = self.metadata
        return out

class ToolSuccessResult(ToolResult):
    """工具执行成功结果。若设置 metadata.truncated，则调用方不再做统一截断。"""
    def __init__(self, result: Any, metadata: Optional[Dict[str, Any]] = None):
        super().__init__(ToolResultStatus.EXECUTE_SUCCESS, result, metadata=metadata)

class ToolTimeoutResult(ToolResult):
    """工具执行超时结果"""
    def __init__(self, result: Any):
        super().__init__(ToolResultStatus.EXECUTE_TIMEOUT, result)

class ToolErrorResult(ToolResult):
    """工具执行错误结果"""
    def __init__(self, result: Any):
        super().__init__(ToolResultStatus.EXECUTE_ERROR, result)

class ToolCancelledResult(ToolResult):
    """工具执行取消结果"""
    def __init__(self, result: Any):
        super().__init__(ToolResultStatus.EXECUTE_CANCELLED, result)


class ToolAbortedResult(ToolResult):
    """Run 级 Abort 跳过的工具结果（用户中断、抢占、父 Agent 中止等）。

    与 ToolCancelledResult 区分：Aborted 表示整轮 run 已 request_abort，
    该 tool 未真正执行；Cancelled 保留给工具内部的取消语义（若有）。
    """
    def __init__(self, result: Any, metadata: Optional[Dict[str, Any]] = None):
        super().__init__(ToolResultStatus.EXECUTE_ABORTED, result, metadata=metadata)
