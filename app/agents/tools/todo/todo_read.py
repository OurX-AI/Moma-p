import json
from pathlib import Path
from typing import Any,Dict,List,Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from .utils import todo_file


def _load(path : Path)->List[Dict[str,Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")) or []
    except Exception:
        return []


@register_tool(name="todo_read", toolset="todo")
class TodoReadTool(BaseTool):

    @property
    def name(self)->str:
        return "todo_read"

    def description(self, params=None) -> str:
        return """Read the current session todo list.

When to use:
- Before continuing a multi-step task that already has todos.
- When the user asks about remaining work / progress.

When NOT to use:
- Simple tasks with no todo list.
- Every message by default — only when tracking is active or needed.

Execution rules:
- No parameters; leave arguments empty.
- Returns items with status/priority/content; empty list if none exist."""

    @property
    def parameters(self)->Dict[str,Any]:
        return {"type":"object","properties":{}}

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
    ) -> ToolResult:
        session_id = agent_ctx.session_id or ""
        if not session_id:
            return ToolErrorResult("todo_read: session_id is required")

        p = todo_file(session_id)
        todos = _load(p)
        remaining = len([t for t in todos if t.get("status")!="completed"])
        output="\n".join([
            f"<path>{p.resolve()}</path>",
            f"<remaining>{remaining}</remaining>",
            "<todos>",
            json.dumps(todos,ensure_ascii=False,indent=2),
            "</todos>",
        ])
        return ToolSuccessResult(output)

