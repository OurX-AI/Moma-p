from typing import Any, Dict, Optional
from ..base import BaseTool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from ...mcp.bridge import mcp_bridge_from_run_ctx


class MCPListPromptsTool(BaseTool):

    @property
    def name(self) -> str:
        return "mcp_list_prompts"

    def description(self, params=None) -> str:
        return """List prompt templates from connected MCP servers.

When to use:
- Discover MCP prompt names/args before `mcp_get_prompt`.

When NOT to use:
- Local agent prompts (AGENT.md etc. are injected automatically).

Failure recovery:
- Empty list -> check MCP connection / server_id filter."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_id": {
                    "type": "string",
                    "description": "Optional MCP server id from config. Omit to list all servers with prompts enabled.",
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
            text = await bridge.list_prompts(server_id, run_ctx=run_ctx)
            if text is None:
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                return ToolErrorResult("mcp_list_prompts failed: no result")
            return ToolSuccessResult(text)
        except Exception as e:
            return ToolErrorResult(f"mcp_list_prompts failed: {e}")


class MCPGetPromptTool(BaseTool):

    @property
    def name(self) -> str:
        return "mcp_get_prompt"

    def description(self, params=None) -> str:
        return """Fetch a filled prompt from an MCP server (messages with role/content).

When to use:
- After `mcp_list_prompts` to get a named template with optional arguments.

When NOT to use:
- Local task instructions already in system prompt.

Failure recovery:
- Missing args / unknown name -> list prompts again and supply required arguments."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Prompt template name from mcp_list_prompts.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Optional key-value arguments required by the prompt template.",
                    "additionalProperties": {"type": "string"},
                },
                "server_id": {
                    "type": "string",
                    "description": "Optional MCP server id when multiple servers expose prompts.",
                },
            },
            "required": ["name"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        server_id: Optional[str] = None,
    ) -> ToolResult:
        bridge = mcp_bridge_from_run_ctx(run_ctx)
        if bridge is None or not bridge.servers:
            return ToolErrorResult("No MCP servers connected")
        try:
            text = await bridge.get_prompt(name, arguments, server_id, run_ctx=run_ctx)
            if text is None:
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                return ToolErrorResult("mcp_get_prompt failed: no result")
            if text.startswith("Failed") or text == "name is required":
                return ToolErrorResult(text)
            return ToolSuccessResult(text)
        except Exception as e:
            return ToolErrorResult(f"mcp_get_prompt failed: {e}")
