import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from app.agents.contants import DEFAULT_AGENT_TYPE
from app.agents.core.react import ReActAgent
from app.agents.core.run_abort import AbortReason
from app.agents.sessions.manager import SESSION_MANAGER


class AgentRunner:
    def __init__(self) -> None:
        self._agents: Dict[str, ReActAgent] = {}
        self._current_session_id: Optional[str] = None
        self._queues: Dict[str, asyncio.Queue[Optional[Tuple[str, bool]]]] = {}
        self._workers: Dict[str, asyncio.Task[None]] = {}

    @property
    def current_session_id(self) -> Optional[str]:
        return self._current_session_id

    def bind_session(self, session_id: Optional[str]) -> None:
        """绑定当前 session；传 None 表示切到临时 session（未落库，等首条消息时再创建）。"""
        self._current_session_id = session_id

    def drop_agent(self, session_id: str) -> None:
        self._agents.pop(session_id, None)

    async def create_session(
        self,
        *,
        user_id: str,
        workspace_path: str,
        agent_type: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
    ) -> str:
        session_id = await SESSION_MANAGER.create_session(
            user_id=user_id,
            agent_type=agent_type or DEFAULT_AGENT_TYPE,
            channel_type="cli",
            workspace_path=workspace_path,
            llm_provider=llm_provider,
            llm_model=llm_model,
        )
        self.bind_session(session_id)
        return session_id

    async def _get_agent(self, session_id: str) -> ReActAgent:
        session = await SESSION_MANAGER.get_session(session_id)
        if session is None:
            raise ValueError(f"Session not found: {session_id}")
        agent = self._agents.get(session_id)
        if agent is not None:
            await SESSION_MANAGER.update_session(
                session_id,
                agent_type=session.agent_type,
                llm_provider=session.llm_provider,
                llm_model=session.llm_model,
                workspace_path=session.workspace_path,
            )
            return agent
        agent = ReActAgent(
            user_id=session.user_id,
            session_id=session_id,
            channel_type="cli",
            channel_id=session_id,
            agent_type=session.agent_type or DEFAULT_AGENT_TYPE,
            workspace_path=session.workspace_path or str(Path.cwd()),
            llm_provider=session.llm_provider,
            llm_model=session.llm_model,
        )
        self._agents[session_id] = agent
        return agent

    def _ensure_worker(self, session_id: str) -> asyncio.Queue[Optional[Tuple[str, bool]]]:
        queue = self._queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[session_id] = queue
            self._workers[session_id] = asyncio.create_task(
                self._session_worker(session_id, queue),
                name=f"cli_session_worker:{session_id}",
            )
        return queue

    async def _session_worker(
        self,
        session_id: str,
        queue: asyncio.Queue[Optional[Tuple[str, bool]]],
    ) -> None:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                content, is_internal = item
                agent = await self._get_agent(session_id)
                await agent.run(original_question=content, is_internal=is_internal)
            except Exception as exc:
                logging.error("CLI session worker error session=%s: %s", session_id, exc)
                await self._emit_worker_error(session_id, exc)
            finally:
                queue.task_done()

    async def _emit_worker_error(self, session_id: str, exc: Exception) -> None:
        from app.agents.output import OutboundMessage, OutboundMessageType, emit_output

        session = await SESSION_MANAGER.get_session(session_id)
        user_id = session.user_id if session else "cli"
        await emit_output(OutboundMessage(
            session_id=session_id,
            user_id=user_id,
            content=f"Agent 执行失败: {exc}",
            outbound_type=OutboundMessageType.RESPONSE,
        ))
        await emit_output(OutboundMessage(
            session_id=session_id,
            user_id=user_id,
            content="",
            outbound_type=OutboundMessageType.RUN_END,
        ))

    async def run_turn(self, session_id: str, content: str, *, is_internal: bool = False) -> None:
        queue = self._ensure_worker(session_id)
        await queue.put((content, is_internal))
        await queue.join()

    async def handle_internal_message(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "").strip()
        content = str(kwargs.get("content") or "").strip()
        if not session_id or not content:
            return
        logging.info("收到 SubAgent 内部消息，session=%s", session_id)
        queue = self._ensure_worker(session_id)
        await queue.put((content, True))

    async def stop_current(self, *, hard: bool = False) -> str:
        session_id = self._current_session_id
        if not session_id:
            return "当前无运行中的会话。"
        agent = self._agents.get(session_id)
        if agent is None or agent.get_state().value == "IDLE":
            return "当前无运行中的 Agent。"
        if hard:
            agent.request_abort(AbortReason.USER_INTERRUPT, "hard kill")
            return "已请求强制终止当前任务。"
        agent.request_abort(AbortReason.USER_INTERRUPT)
        return "已请求终止当前任务（等待协作退出）。"

    def status_text(self) -> str:
        session_id = self._current_session_id or "(none)"
        agent = self._agents.get(self._current_session_id or "")
        state = agent.get_state().value if agent is not None else "N/A"
        workspace = str(agent.workspace_path) if agent is not None else str(Path.cwd())
        pending = self._queues.get(self._current_session_id or "")
        pending_count = pending.qsize() if pending is not None else 0
        return (
            f"**CLI 状态**\n"
            f"- session: {session_id}\n"
            f"- agent_state: {state}\n"
            f"- workspace: {workspace}\n"
            f"- pending_messages: {pending_count}\n"
            f"- cached_agents: {len(self._agents)}"
        )

    def agent_state(self, session_id: str) -> str:
        agent = self._agents.get(session_id)
        if agent is None:
            return "IDLE"
        return agent.get_state().value


AGENT_RUNNER = AgentRunner()
