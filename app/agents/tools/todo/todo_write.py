import json
from pathlib import Path
from typing import Any, Dict, List
from ..base import BaseTool
from ..catalog import register_tool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from .utils import todo_file
from .verification_nudge import TodoVerificationNudge


def _save(path: Path, todos: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")


@register_tool(name="todo_write", toolset="todo")
class TodoWriteTool(BaseTool):

    @property
    def name(self) -> str:
        return "todo_write"

    def description(self, params=None) -> str:
        return """Create/replace the session todo list for multi-step work tracking.

When to use:
- Complex tasks with 3+ distinct steps or multiple modules.
- User explicitly asks for a todo list / provides multiple tasks.
- Non-trivial implement/fix work: include an explicit verification step
  (e.g. "Run related tests" or "spawn verification") as the last item.

When NOT to use:
- Single trivial file change or short conversational answers.
- Tasks completable in fewer than 3 trivial steps.

Execution rules:
- Keep at most ONE item `in_progress`.
- Mark `completed` only after the work (including required verification) is actually done.
- Cancel obsolete items; full-list replace: pass the complete updated `todos` array.
- For 3+ step implement tasks, the list should end with a verification item.

Failure recovery:
- Lost track of progress -> `todo_read` then rewrite a clean list."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Brief description of the task",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Current status of the task",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Priority level of the task",
                            },
                        },
                        "required": ["content", "status", "priority"],
                    },
                    "description": "The updated todo list",
                },
            },
            "required": ["todos"],
        }

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        return False

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        todos: List[Dict[str, Any]],
    ) -> ToolResult:
        session_id = agent_ctx.session_id or ""
        if not session_id:
            return ToolErrorResult("todo_write: session_id is required")

        p = todo_file(session_id)
        _save(p, todos)
        remaining = len([t for t in todos if t.get("status") != "completed"])
        output = "\n".join(
            [
                f"<path>{p.resolve()}</path>",
                f"<remaining>{remaining}</remaining>",
                "<todos>",
                json.dumps(todos, ensure_ascii=False, indent=2),
                "</todos>",
            ]
        )

        # 判断是否需要提示 spawn(verification)
        nudge = TodoVerificationNudge.needed(
            todos,
            is_subagent=bool(agent_ctx.is_subagent),
        )
        if nudge:
            output += TodoVerificationNudge.NOTE  # 提示 spawn(verification)
        return ToolSuccessResult(output)
