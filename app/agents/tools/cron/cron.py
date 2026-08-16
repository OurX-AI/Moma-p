"""Cron 工具：增/删/改/查定时任务。"""
from datetime import datetime
from typing import Any, Dict, Optional
from ..base import BaseTool
from ..catalog import register_tool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from app.services.cron import CRON_MANAGER, CronKind, CronPayload, CronSchedule
from app.services.cron.manager import validate_cron_expr


@register_tool(name="cron", toolset="cron")
class CronTool(BaseTool):
    """定时任务工具：支持 add/list/remove/update/run_now。创建任务时使用当前会话的 user_id/channel_id；查看、修改、删除仅限当前用户的任务。"""
    def __init__(self, agent_ctx: AgentContext | None = None) -> None:
        super().__init__(agent_ctx=agent_ctx)
        self._cron_manager = CRON_MANAGER

    @property
    def name(self) -> str:
        return "cron"

    def description(self, params=None) -> str:
        return """Schedule reminders and recurring agent tasks.

When to use:
- User asks for reminders / recurring jobs (add/list/remove/update/run_now).

When NOT to use:
- One-off immediate work in the current turn (just execute it).

Execution rules:
- durable=false (default) is session-only; durable=true persists to disk only when explicitly needed.
- list/remove/update/run_now apply only to the current user's jobs.
- Schedule via every_seconds, cron_expr (+ optional tz), or at (ISO one-shot).

Failure recovery:
- Invalid cron_expr -> fix expression then retry add/update.
- Missing job -> list first, then remove/update with correct id."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove", "update", "run_now"],
                    "description": "add=create, list=query, remove=delete, update=enable/disable or message, run_now=execute now",
                },
                "message": {
                    "type": "string",
                    "description": "Reminder/task message (for add); or new message (for update)",
                },
                "every_seconds": {
                    "type": "integer",
                    "description": "Interval in seconds for recurring task (for add)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression e.g. '0 9 * * *' (for add)",
                },
                "tz": {
                    "type": "string",
                    "description": "IANA timezone for cron e.g. America/Vancouver (for add with cron_expr)",
                },
                "at": {
                    "type": "string",
                    "description": "ISO datetime for one-time run e.g. 2026-02-12T10:30:00 (for add)",
                },
                "recurring": {
                    "type": "boolean",
                    "description": "For cron_expr: true (default)=keep firing; false=one-shot then auto-delete",
                    "default": True,
                },
                "durable": {
                    "type": "boolean",
                    "description": "true=persist across restarts; false (default)=session-only in memory",
                    "default": False,
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for remove, update, or run_now)",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Enable or disable job (for update)",
                },
                "kind": {
                    "type": "string",
                    "enum": ["remind", "agent"],
                    "description": "Task type: remind=notify user, agent=run agent (for add)",
                },
            },
            "required": ["action"],
        }

    def is_readonly(self, params=None) -> bool:
        if isinstance(params, dict) and str(params.get("action") or "").lower() == "list":
            return True
        return False

    def is_parallel(self, params=None) -> bool:
        return self.is_readonly(params)

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        action: str,
        message: str = "",
        every_seconds: Optional[int] = None,
        cron_expr: Optional[str] = None,
        tz: Optional[str] = None,
        at: Optional[str] = None,
        recurring: Optional[bool] = None,
        durable: Optional[bool] = None,
        job_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        kind: str = "remind",
        **kwargs: Any,
    ) -> ToolResult:
        if action == "add":
            return await self._add(
                agent_ctx,
                run_ctx,
                message,
                every_seconds,
                cron_expr,
                tz,
                at,
                kind,
                recurring=True if recurring is None else bool(recurring),
                durable=False if durable is None else bool(durable),
            )
        if action == "list":
            return await self._list(agent_ctx, run_ctx)
        if action == "remove":
            return await self._remove(agent_ctx, run_ctx, job_id)
        if action == "update":
            return await self._update(agent_ctx, run_ctx, job_id, enabled, message)
        if action == "run_now":
            return await self._run_now(agent_ctx, run_ctx, job_id)
        return ToolErrorResult(f"Unknown action: {action}")

    async def _add(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        message: str,
        every_seconds: Optional[int],
        cron_expr: Optional[str],
        tz: Optional[str],
        at: Optional[str],
        kind: str,
        *,
        recurring: bool = True,
        durable: bool = False,
    ) -> ToolResult:
        if not message and kind == "remind":
            return ToolErrorResult("message is required for add (remind)")
        if tz and not cron_expr:
            return ToolErrorResult("tz can only be used with cron_expr")
        if tz:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(tz)
            except Exception:
                return ToolErrorResult(f"Unknown timezone: {tz!r}")

        delete_after = False
        if every_seconds is not None and every_seconds > 0:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            err = validate_cron_expr(cron_expr, tz)
            if err:
                return ToolErrorResult(err)
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
            delete_after = not recurring
        elif at:
            try:
                dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
                at_ms = int(dt.timestamp() * 1000)
            except Exception:
                return ToolErrorResult(f"Invalid at datetime: {at!r}")
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return ToolErrorResult("One of every_seconds, cron_expr, or at is required")

        kind_enum = CronKind.AGENT if kind == "agent" else CronKind.REMIND
        payload = CronPayload(
            kind=kind_enum,
            message=message,
            trigger_session_id=agent_ctx.session_id,
            need_deliver=(kind_enum == CronKind.REMIND),
            user_id=agent_ctx.user_id,
            channel_type=agent_ctx.channel_type,
            channel_id=agent_ctx.channel_id or "",
            agent_type=agent_ctx.agent_type,
        )
        try:
            job = await self._cron_manager.add_job(
                name=(message or "cron")[:30],
                schedule=schedule,
                payload=payload,
                enabled=True,
                delete_after_run=delete_after,
                durable=durable,
            )
        except ValueError as e:
            return ToolErrorResult(str(e))
        where = "durable" if job.durable else "session-only"
        oneshot = ", one-shot" if job.delete_after_run else ""
        return ToolSuccessResult(
            f"Created job '{job.name}' (id: {job.id}, {where}{oneshot})"
        )

    async def _list(self, agent_ctx: AgentContext, run_ctx: RuntimeContext) -> ToolResult:
        jobs = await self._cron_manager.list_jobs(user_id=agent_ctx.user_id)
        if not jobs:
            return ToolSuccessResult("No scheduled jobs for current user.")
        lines = []
        for j in jobs:
            dur = "durable" if j.durable else "session-only"
            oneshot = ", one-shot" if j.delete_after_run else ""
            lines.append(
                f"- {j.name} (id: {j.id}, schedule: {j.schedule.kind}, "
                f"enabled: {j.enabled}, {dur}{oneshot})"
            )
        return ToolSuccessResult("Scheduled jobs:\n" + "\n".join(lines))

    async def _remove(self, agent_ctx: AgentContext, run_ctx: RuntimeContext, job_id: Optional[str]) -> ToolResult:
        if not job_id:
            return ToolErrorResult("job_id is required for remove")
        ok = await self._cron_manager.remove_job(job_id, user_id=agent_ctx.user_id)
        if ok:
            return ToolSuccessResult(f"Removed job {job_id}")
        return ToolErrorResult(f"Job {job_id} not found or does not belong to current user")

    async def _update(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        job_id: Optional[str],
        enabled: Optional[bool],
        message: Optional[str],
    ) -> ToolResult:
        if not job_id:
            return ToolErrorResult("job_id is required for update")
        job = await self._cron_manager.get_job(job_id, user_id=agent_ctx.user_id)
        if not job:
            return ToolErrorResult(f"Job {job_id} not found or does not belong to current user")
        if enabled is not None:
            job.enabled = enabled
        if message is not None:
            job.payload.message = message
        if enabled is not None or message is not None:
            await self._cron_manager.update_job(job, user_id=agent_ctx.user_id)
        return ToolSuccessResult(f"Updated job {job_id}")

    async def _run_now(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        job_id: Optional[str],
    ) -> ToolResult:
        if not job_id:
            return ToolErrorResult("job_id is required for run_now")
        ok = await self._cron_manager.run_job_now(job_id, user_id=agent_ctx.user_id)
        if ok:
            return ToolSuccessResult(f"Triggered job {job_id} now")
        return ToolErrorResult(f"Job {job_id} not found or does not belong to current user")
