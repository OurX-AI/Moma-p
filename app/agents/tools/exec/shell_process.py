import json
import time
from typing import Any
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from .process_manager import PROCESS_MANAGER
from .output import OutputFormatter
from ..result_truncate_policy import ToolResultTruncateSpec


@register_tool(name="shell_process", toolset="exec")
class ShellProcessTool(BaseTool):
    """管理 bash / powershell(background=true) 启动的后台进程。"""

    @property
    def name(self) -> str:
        return "shell_process"

    def description(self, params=None) -> str:
        return """Manage background processes started by bash/powershell(background=true).

When to use:
- Wait for / inspect / kill long-running servers and background jobs.

When NOT to use:
- Start commands (use bash/powershell with background=true).
- One-shot foreground commands (use bash/powershell without background).

Actions:
- list: session background processes
- wait: block until exit (optional timeout ms) — prefer when you need the final result
- poll: incremental stdout/stderr — occasional progress only, no tight loops
- log: full accumulated output so far
- kill: terminate

Failure recovery:
- Missing session_id -> `list` then retry with the correct id.
- Still running too long -> `poll`/`log` for progress, then `wait` with larger timeout or `kill` if stuck."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "poll", "log", "wait", "kill"],
                    "description": "Process management action",
                },
                "session_id": {
                    "type": "string",
                    "description": "Background session id returned by bash/powershell",
                },
                "timeout": {
                    "type": "number",
                    "description": "For wait: timeout in milliseconds",
                },
            },
            "required": ["action"],
        }

    def is_readonly(self, params=None) -> bool:
        params = params or {}
        action = str(params.get("action") or "").strip().lower()
        return action in {"list", "poll", "log"}

    def is_parallel(self, params=None) -> bool:
        params = params or {}
        action = str(params.get("action") or "").strip().lower()
        return action in {"list", "poll", "log"}

    def result_truncate_spec(self) -> ToolResultTruncateSpec:
        return ToolResultTruncateSpec(
            max_bytes=OutputFormatter.MAX_CHARS,
            direction="tail",
        )

    def validate_params(self, params: dict) -> list[str]:
        errors = super().validate_params(params)
        action = str(params.get("action") or "").strip().lower()
        if action and action != "list" and not str(params.get("session_id") or "").strip():
            errors.append(
                "[MISSING_REQUIRED] session_id is required for action "
                f"`{action}`. Pass the session_id from bash/powershell(background=true) and retry."
            )
        timeout = params.get("timeout")
        if timeout is not None and isinstance(timeout, (int, float)) and timeout < 0:
            errors.append(
                "[OUT_OF_RANGE] timeout must be >= 0 (milliseconds). "
                "Omit timeout or pass a positive value and retry."
            )
        return errors

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        action: str,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        action_name = (action or "").strip().lower()
        agent_session_id = agent_ctx.session_id or ""

        if action_name == "list":
            sessions = await PROCESS_MANAGER.list_sessions(agent_session_id)
            items = []
            for s in sessions:
                end = s.finished_at if s.finished_at is not None else time.time()
                item = {
                    "session_id": s.session_id,
                    "pid": s.process.pid,
                    "status": s.status,
                    "command": s.command,
                    "cwd": s.cwd,
                    "returncode": s.returncode,
                    "started_at": s.started_at,
                    "elapsed_ms": max(0, int((end - s.started_at) * 1000)),
                }
                if s.finished_at is not None:
                    item["finished_at"] = s.finished_at
                items.append(item)
            return ToolSuccessResult(json.dumps({"processes": items}, ensure_ascii=False))

        if not session_id:
            return ToolErrorResult("session_id is required for this action")

        owned = await PROCESS_MANAGER.get(session_id)
        if owned is None:
            return ToolErrorResult(json.dumps({"error": f"Unknown session_id: {session_id}"}, ensure_ascii=False))
        if owned.agent_session_id:
            if not agent_session_id or owned.agent_session_id != agent_session_id:
                return ToolErrorResult(json.dumps({"error": "session_id not found in this agent session"}, ensure_ascii=False))

        timeout_sec = None
        if timeout is not None:
            if timeout < 0:
                return ToolErrorResult(f"Invalid timeout value: {timeout}")
            timeout_sec = max(1, int(float(timeout) / 1000))

        if action_name == "poll":
            result = await PROCESS_MANAGER.poll(session_id)
        elif action_name == "log":
            result = await PROCESS_MANAGER.log(session_id)
        elif action_name == "wait":
            result = await PROCESS_MANAGER.wait(session_id, timeout_sec, run_ctx=run_ctx)
            if result.get("aborted"):
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                return ToolErrorResult("shell_process wait failed: no result")
        elif action_name == "kill":
            result = await PROCESS_MANAGER.kill(session_id)
        else:
            return ToolErrorResult(f"Unknown action: {action}")

        if result.get("error"):
            return ToolErrorResult(json.dumps(result, ensure_ascii=False))

        for key in ("stdout", "stderr"):
            if key in result and isinstance(result[key], str):
                result[key] = OutputFormatter.truncate(
                    result[key],
                    prefer_tail=True,
                )

        return ToolSuccessResult(json.dumps(result, ensure_ascii=False))
