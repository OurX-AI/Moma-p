from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional, Tuple
if TYPE_CHECKING:
    from .core.run_abort import RunAbortController
    from .sessions.message import Message
    from .tools.schemes import ToolAbortedResult

NotifyUserCallback = Callable[["Message"], Awaitable[None]]
PushHistoryMessageCallback = Callable[["Message"], Awaitable[None]]

@dataclass
class AgentContext:
    """单次 Agent 运行的公共快照，供 Memory、Context、Tools 等从同一来源取数。"""

    user_id: str = ""
    session_id: str = ""
    agent_type: str = ""
    agent_description: str = ""
    agent_path: str = ""
    workspace_path: str = ""
    channel_type: str = ""
    channel_id: str = ""
    llms_list: List[Tuple[str, str]] = field(default_factory=list)
    is_subagent: bool = False
    parent_agent_type: str = ""
    agent_config: dict[str, Any] = field(default_factory=dict)
    skills_manager: Any = field(default=None, compare=False, repr=False)
    subagent_manager: Any = field(default=None, compare=False, repr=False)
    params: dict[str, Any] = field(default_factory=dict)

    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)


@dataclass
class RuntimeContext:
    """运行时上下文"""
    last_llm: Any = field(default=None, compare=False, repr=False)
    mcp_bridge: Any = field(default=None, compare=False, repr=False)
    abort_controller: Optional["RunAbortController"] = None
    params: dict[str, Any] = field(default_factory=dict)
    repo_id: Optional[str] = None
    actor_id: str = ""

    # 回调处理
    notify_user_callback: Optional[NotifyUserCallback] = field(default=None, compare=False, repr=False)
    push_history_callback: Optional[PushHistoryMessageCallback] = field(default=None, compare=False, repr=False)

    def is_aborted(self) -> bool:
        """本轮 run 是否已 request_abort；Tool / Factory 侧统一检查入口。"""
        ctrl = self.abort_controller
        return ctrl is not None and ctrl.is_aborted()

    def aborted_tool_result(self, tool_name: str) -> "ToolAbortedResult":
        """构造 Run 级 Abort 的 ToolAbortedResult，metadata 携带 abort_reason 供 UI/日志。"""
        from .tools.schemes import ToolAbortedResult
        ctrl = self.abort_controller
        if ctrl is not None:
            detail = ctrl.message or ctrl.reason_label()
            text = f"[Tool execution aborted — {tool_name} was skipped due to {detail}]"
        else:
            text = f"[Tool execution aborted — {tool_name} was skipped]"
        metadata = None
        if ctrl is not None and ctrl.reason is not None:
            metadata = {"aborted": True, "abort_reason": ctrl.reason.value}
        return ToolAbortedResult(text, metadata=metadata)