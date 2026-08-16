from typing import Any, Dict, Optional
from ..base import BaseTool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from ...mcp.bridge import mcp_bridge_from_run_ctx


class MCPListResourcesTool(BaseTool):

    @property
    def name(self) -> str:
        return "mcp_list_resources"

    def description(self, params=None) -> str:
        return """List read-only resources from connected MCP servers.

When to use:
- Discover MCP docs/schemas/URIs before `mcp_read_resource`.

When NOT to use:
- Calling deferred MCP tools (use `mcp_search_tools` + activate).

Failure recovery:
- Empty list -> check MCP connection / server_id filter."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_id": {
                    "type": "string",
                    "description": "Optional MCP server id from config (e.g. playwright). Omit to list all servers.",
                },
            },
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        server_id: Optional[str] = None,
    ) -> ToolResult:
        bridge = mcp_bridge_from_run_ctx(run_ctx)
        if bridge is None or not bridge.servers:
            return ToolErrorResult("No MCP servers connected")
        try:
            text = await bridge.list_resources(server_id, run_ctx=run_ctx)
            if text is None:
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                return ToolErrorResult("mcp_list_resources failed: no result")
            return ToolSuccessResult(text)
        except Exception as e:
            return ToolErrorResult(f"mcp_list_resources failed: {e}")


class MCPReadResourceTool(BaseTool):

    @property
    def name(self) -> str:
        return "mcp_read_resource"

    def description(self, params=None) -> str:
        return """Read a resource URI from an MCP server.

When to use:
- After `mcp_list_resources` returns a concrete uri.

When NOT to use:
- Local workspace files (use `read_file`).

Failure recovery:
- Unknown uri -> `mcp_list_resources` then retry; set server_id if ambiguous."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Resource URI returned by mcp_list_resources.",
                },
                "server_id": {
                    "type": "string",
                    "description": "Optional MCP server id when multiple servers expose the same URI pattern.",
                },
            },
            "required": ["uri"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        uri: str,
        server_id: Optional[str] = None,
    ) -> ToolResult:
        bridge = mcp_bridge_from_run_ctx(run_ctx)
        if bridge is None or not bridge.servers:
            return ToolErrorResult("No MCP servers connected")
        try:
            text = await bridge.read_resource(uri, server_id, run_ctx=run_ctx)
            if text is None:
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                return ToolErrorResult("mcp_read_resource failed: no result")
            if text.startswith("Failed") or text == "uri is required":
                return ToolErrorResult(text)
            return ToolSuccessResult(text)
        except Exception as e:
            return ToolErrorResult(f"mcp_read_resource failed: {e}")
