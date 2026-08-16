from __future__ import annotations
import json
from typing import Any
from ..base import BaseTool
from ..catalog import register_tool
from ..policy import SPAWN_STATUS_TOOL_NAME, SUBAGENT_TOOLSET
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from ...core.subagent_task import SubAgentTaskStatus


@register_tool(name=SPAWN_STATUS_TOOL_NAME, toolset=SUBAGENT_TOOLSET)
class SpawnStatusTool(BaseTool):
    """查询 spawn 子任务登记表（list / get）。"""

    @property
    def name(self) -> str:
        return SPAWN_STATUS_TOOL_NAME

    def description(self, params=None) -> str:
        return """Inspect subagent tasks started by spawn.

When to use:
- Explicit status check for async/sync spawn tasks (`list` / `get`).

When NOT to use:
- Normal completion path — main agent auto-ingests async results; do not poll as primary flow.

Actions:
- list: recent tasks (optional status/mode filters)
- get: one task by task_id

Failure recovery:
- Unknown task_id -> `list` then retry with a valid id."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get"],
                    "description": "list=enumerate tasks; get=fetch one by task_id.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Required when action=get. The id returned by spawn(mode='async').",
                },
                "status": {
                    "type": "string",
                    "enum": [s.value for s in SubAgentTaskStatus],
                    "description": "Optional filter for action=list.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["sync", "async"],
                    "description": "Optional filter for action=list.",
                },
            },
            "required": ["action"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        action: str,
        *,
        task_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
    ) -> ToolResult:
        manager = agent_ctx.subagent_manager
        if manager is None:
            return ToolErrorResult("spawn_status is not available: subagent_manager is not configured")
        
        act = (action or "").strip().lower()
        if act == "list":
            records = manager.list_task_records(status=status, mode=mode)
            payload = {
                "count": len(records),
                "tasks": [r.to_summary() for r in records],
            }
            return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))

        if act == "get":
            key = (task_id or "").strip()
            if not key:
                return ToolErrorResult("task_id is required when action=get")
            record = manager.get_task_record(key)
            if record is None:
                return ToolErrorResult(f"unknown task_id: {key}")
            return ToolSuccessResult(json.dumps(record.to_detail(), ensure_ascii=False, indent=2))
        return ToolErrorResult("action must be 'list' or 'get'")
