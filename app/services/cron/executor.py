"""Cron 到期执行：REMIND 走 emit_output；AGENT 投递到 CLI AgentRunner。"""
import asyncio
import logging
from pathlib import Path
from app.agents.contants import DEFAULT_AGENT_TYPE
from app.agents.output import OutboundMessage, OutboundMessageType, emit_output
from app.agents.sessions.message import Message
from app.agents.sessions.manager import SESSION_MANAGER
from .types import CronJob, CronKind


async def default_on_execute(job: CronJob) -> None:
    """
    定时任务到期时的预置执行逻辑。
    - REMIND: 通过 emit_output 推送给当前 CLI/TUI；有会话则写入历史。
    - AGENT: 确保会话存在后投递到 AGENT_RUNNER（异步跑，不阻塞调度循环）。
    """
    payload = job.payload
    if payload.kind == CronKind.REMIND:
        if not payload.need_deliver:
            logging.debug("Cron job %s REMIND need_deliver=False, skip", job.id)
            return
        user_id = payload.user_id or "cron"
        session_id = payload.trigger_session_id or ""
        text = "定时提醒：" + (payload.message or "")
        await emit_output(
            OutboundMessage(
                session_id=session_id or job.id,
                user_id=user_id,
                content=text,
                outbound_type=OutboundMessageType.RESPONSE,
            )
        )
        if session_id:
            await SESSION_MANAGER.add_message(session_id, Message.assistant_message(text))
        logging.info("Cron job %s REMIND delivered session_id=%s", job.id, session_id)
        return

    if payload.kind == CronKind.AGENT:
        from app.cli.runner import AGENT_RUNNER

        session_id = (payload.trigger_session_id or "").strip()
        if not session_id:
            session_id = await SESSION_MANAGER.create_session(
                user_id=payload.user_id or "cron",
                agent_type=payload.agent_type or DEFAULT_AGENT_TYPE,
                channel_type=payload.channel_type or "cli",
                description=job.name or "cron",
                workspace_path=str(Path.cwd()),
            )

        content = payload.message or ""
        if payload.extra:
            parts = [content] if content else []
            for k, v in payload.extra.items():
                parts.append(f"{k}: {v}")
            content = "\n".join(parts)
        content = content or "执行定时任务"

        asyncio.create_task(
            AGENT_RUNNER.run_turn(session_id, content, is_internal=True),
            name=f"cron_agent:{job.id}",
        )
        logging.info("Cron job %s AGENT enqueued session_id=%s", job.id, session_id)
        return

    logging.warning("Cron job %s unknown kind %s", job.id, getattr(payload.kind, "value", payload.kind))
