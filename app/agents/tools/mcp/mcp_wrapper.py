"""MCP 单个工具的执行包装，将 MCP 工具适配为 BaseTool。"""
import asyncio
import logging
from typing import Any, Dict
from ..base import BaseTool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ..utils import await_with_abort
from ...schemes import AgentContext, RuntimeContext


def mcp_registry_tool_name(server_id: str, original_tool_name: str) -> str:
    """Factory 内注册名（带 server 前缀），避免与本地同名工具冲突。"""
    return f"mcp_{server_id}_{original_tool_name}"


def _mcp_tool_to_schema(tool_def: Any) -> Dict[str, Any]:
    """将 MCP Tool 的 inputSchema 转为 LLM 用的 parameters 结构（含 type/properties/required）。"""
    schema = getattr(tool_def, "inputSchema", None) or {}
    if isinstance(schema, dict):
        return dict(schema)
    return {"type": "object", "properties": {}, "required": []}


class MCPToolWrapper(BaseTool):
    """将 MCP 服务暴露的单个工具包装为 BaseTool，便于注册到 Agent。"""

    def __init__(
        self,
        mcp_client_session: Any,
        server_id: str,
        tool_def: Any,
        timeout_seconds: float | None = None,
        agent_ctx: AgentContext | None = None,
    ):
        super().__init__(agent_ctx=agent_ctx)
        self._mcp_client_session = mcp_client_session
        self._timeout = timeout_seconds
        self._original_tool_name = getattr(tool_def, "name", "") or "mcp_tool"
        self._tool_name = mcp_registry_tool_name(server_id, self._original_tool_name)
        self._description = getattr(tool_def, "description", None) or ""
        self._parameters = _mcp_tool_to_schema(tool_def)

    @property
    def name(self) -> str:
        """Factory / LLM 侧工具名（带 server 前缀）。"""
        return self._tool_name

    @property
    def original_name(self) -> str:
        """MCP 服务端原始工具名。"""
        return self._original_tool_name

    def description(self, params=None) -> str:
        return self._description

    @property
    def parameters(self) -> Dict[str, Any]:
        return self._parameters

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        return False

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        **kwargs: Any
    ) -> ToolResult:
        try:
            result = await await_with_abort(
                run_ctx,
                self._mcp_client_session.call_tool(
                    self._original_tool_name,
                    arguments=kwargs if kwargs else None,
                    read_timeout_seconds=self._timeout,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception(f"MCP tool {self._tool_name} failed: {e}")
            return ToolErrorResult(str(e))

        if result is None:
            if run_ctx.is_aborted():
                return run_ctx.aborted_tool_result(self.name)
            return ToolErrorResult(f"MCP tool {self._tool_name} failed: no result")

        content = getattr(result, "content", None) or []
        parts = [getattr(c, "text", str(c)) for c in content if hasattr(c, "text")]
        text = "\n".join(parts) if parts else str(result)

        if getattr(result, "is_error", False):
            return ToolErrorResult(text)
        return ToolSuccessResult(text)
