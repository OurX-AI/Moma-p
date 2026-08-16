import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from ...schemes import RuntimeContext
from .common import MAX_BACKGROUND_STREAM_CHARS
from .runtime import ExecKind, ExecRuntime


PRUNE_INTERVAL_SEC = 300
PRUNE_MAX_AGE_SEC = 1800
_WAIT_POLL_SEC = 0.2


@dataclass
class BackgroundSession:
    session_id: str
    agent_session_id: str
    command: str
    cwd: str
    process: asyncio.subprocess.Process
    status: str = "running"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_offset: int = 0
    stderr_offset: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _watch_task: asyncio.Task | None = field(default=None, repr=False)


class BackgroundProcessManager:
    def __init__(
        self,
        *,
        prune_interval_sec: float = PRUNE_INTERVAL_SEC,
        prune_max_age_sec: float = PRUNE_MAX_AGE_SEC,
    ) -> None:
        self._sessions: dict[str, BackgroundSession] = {}
        self._lock = asyncio.Lock()
        self._prune_interval_sec = prune_interval_sec
        self._prune_max_age_sec = prune_max_age_sec
        self._prune_task: Optional[asyncio.Task] = None

    async def start(
        self,
        *,
        command: str,
        cwd: str,
        agent_session_id: str,
        exec_kind: ExecKind,
    ) -> BackgroundSession:
        process = await ExecRuntime.create_process(
            command,
            cwd,
            kind=exec_kind,
        )
        session = BackgroundSession(
            session_id=f"proc_{uuid.uuid4().hex[:12]}",
            agent_session_id=agent_session_id or "",
            command=command,
            cwd=cwd,
            process=process,
        )
        session._watch_task = asyncio.create_task(self._watch(session))
        async with self._lock:
            self._sessions[session.session_id] = session
        return session

    async def get(self, session_id: str) -> BackgroundSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def list_sessions(self, agent_session_id: str) -> list[BackgroundSession]:
        async with self._lock:
            if agent_session_id:
                return [
                    s for s in self._sessions.values()
                    if s.agent_session_id == agent_session_id
                ]
            return list(self._sessions.values())

    async def poll(self, session_id: str) -> dict[str, Any]:
        session = await self.get(session_id)
        if session is None:
            return {"error": f"Unknown session_id: {session_id}"}
        stdout_new = session.stdout[session.stdout_offset:]
        stderr_new = session.stderr[session.stderr_offset:]
        session.stdout_offset = len(session.stdout)
        session.stderr_offset = len(session.stderr)
        return self._session_payload(session, stdout_new=stdout_new, stderr_new=stderr_new)

    async def log(self, session_id: str) -> dict[str, Any]:
        session = await self.get(session_id)
        if session is None:
            return {"error": f"Unknown session_id: {session_id}"}
        return self._session_payload(session, include_full_output=True)

    async def wait(
        self,
        session_id: str,
        timeout_sec: float | None,
        *,
        run_ctx: RuntimeContext | None = None,
    ) -> dict[str, Any]:
        session = await self.get(session_id)
        if session is None:
            return {"error": f"Unknown session_id: {session_id}"}
        if session.status != "running":
            return self._session_payload(session, include_full_output=True)

        wait_task = asyncio.create_task(session.process.wait())
        deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
        try:
            while not wait_task.done():
                if run_ctx is not None and run_ctx.is_aborted():
                    wait_task.cancel()
                    try:
                        await wait_task
                    except asyncio.CancelledError:
                        pass
                    return self._session_payload(
                        session,
                        include_full_output=True,
                        extra={"aborted": True},
                    )
                if deadline is not None and time.monotonic() >= deadline:
                    wait_task.cancel()
                    try:
                        await wait_task
                    except asyncio.CancelledError:
                        pass
                    return self._session_payload(
                        session,
                        include_full_output=True,
                        extra={"timed_out": True},
                    )
                await asyncio.sleep(_WAIT_POLL_SEC)
            await self._finalize(session)
        except asyncio.CancelledError:
            raise
        return self._session_payload(session, include_full_output=True)

    async def kill(self, session_id: str) -> dict[str, Any]:
        session = await self.get(session_id)
        if session is None:
            return {"error": f"Unknown session_id: {session_id}"}
        if session.status == "running":
            session.process.kill()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            session.status = "killed"
            session.returncode = session.process.returncode
            session.finished_at = time.time()
        return self._session_payload(session, include_full_output=True)

    async def prune_finished(self, max_age_sec: float | None = None) -> int:
        age = self._prune_max_age_sec if max_age_sec is None else max_age_sec
        cutoff = time.time() - age
        removed = 0
        async with self._lock:
            stale_ids = [
                sid for sid, s in self._sessions.items()
                if s.status != "running"
                and s.finished_at is not None
                and s.finished_at < cutoff
            ]
            for session_id in stale_ids:
                del self._sessions[session_id]
                removed += 1
        return removed

    def start_prune_loop(self) -> None:
        """定期清理已结束的后台进程注册项（仅内存，不杀 running）。"""
        if self._prune_task is not None and not self._prune_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._prune_interval_sec)
                try:
                    removed = await self.prune_finished()
                    if removed:
                        logging.info(
                            "Background process registry pruned %s finished session(s)",
                            removed,
                        )
                except Exception:
                    logging.exception("Background process prune loop failed")

        self._prune_task = asyncio.create_task(_loop())
        logging.info(
            "Background process prune loop started, interval=%ss, max_age=%ss",
            self._prune_interval_sec,
            self._prune_max_age_sec,
        )

    def stop_prune_loop(self) -> None:
        if self._prune_task is None:
            return
        self._prune_task.cancel()
        self._prune_task = None

    async def shutdown(self) -> None:
        """应用关闭：停止清理循环并终止仍在运行的后台进程。"""
        self.stop_prune_loop()
        async with self._lock:
            running_ids = [
                sid for sid, s in self._sessions.items() if s.status == "running"
            ]
        for session_id in running_ids:
            await self.kill(session_id)
        async with self._lock:
            self._sessions.clear()

    def _session_payload(
        self,
        session: BackgroundSession,
        *,
        stdout_new: str = "",
        stderr_new: str = "",
        include_full_output: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        end = session.finished_at if session.finished_at is not None else time.time()
        elapsed_ms = max(0, int((end - session.started_at) * 1000))
        payload: dict[str, Any] = {
            "session_id": session.session_id,
            "pid": session.process.pid,
            "status": session.status,
            "command": session.command,
            "cwd": session.cwd,
            "returncode": session.returncode,
            "started_at": session.started_at,
            "elapsed_ms": elapsed_ms,
        }
        if session.finished_at is not None:
            payload["finished_at"] = session.finished_at

        if session.stdout_truncated:
            payload["stdout_truncated"] = True
        if session.stderr_truncated:
            payload["stderr_truncated"] = True
        if include_full_output:
            payload["stdout"] = session.stdout
            payload["stderr"] = session.stderr
        else:
            if stdout_new:
                payload["stdout"] = stdout_new
            if stderr_new:
                payload["stderr"] = stderr_new
        if extra:
            payload.update(extra)
        return payload

    def _append_stream(self, session: BackgroundSession, field_name: str, text: str) -> None:
        if field_name == "stdout":
            current = session.stdout
            truncated_flag = "stdout_truncated"
        else:
            current = session.stderr
            truncated_flag = "stderr_truncated"

        if getattr(session, truncated_flag):
            return

        remaining = MAX_BACKGROUND_STREAM_CHARS - len(current)
        if remaining <= 0:
            setattr(session, truncated_flag, True)
            return
        if len(text) > remaining:
            if field_name == "stdout":
                session.stdout += text[:remaining]
            else:
                session.stderr += text[:remaining]
            setattr(session, truncated_flag, True)
            return
        if field_name == "stdout":
            session.stdout += text
        else:
            session.stderr += text

    async def _watch(self, session: BackgroundSession) -> None:
        try:
            await asyncio.gather(
                self._drain_stream(session.process.stdout, session, "stdout"),
                self._drain_stream(session.process.stderr, session, "stderr"),
            )
            await session.process.wait()
            await self._finalize(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("Background process watch failed for %s: %s", session.session_id, e)
            session.status = "error"
            session.returncode = session.process.returncode
            session.finished_at = time.time()

    async def _drain_stream(
        self,
        stream: asyncio.StreamReader | None,
        session: BackgroundSession,
        field_name: str,
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            self._append_stream(session, field_name, text)

    async def _finalize(self, session: BackgroundSession) -> None:
        if session.status == "running":
            session.status = "done"
        session.returncode = session.process.returncode
        if session.finished_at is None:
            session.finished_at = time.time()


PROCESS_MANAGER = BackgroundProcessManager()
