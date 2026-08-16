"""Cron 管理器：任务增删改查与调度循环。"""
import asyncio
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, List, Optional
from croniter import croniter
from .executor import default_on_execute
from .store import CronFileStore, CronStore
from .types import CronJob, CronJobState, CronPayload, CronSchedule


POLL_INTERVAL_SEC = 60.0
MAX_JOBS = 50


def _now_ms() -> int:
    return int(time.time() * 1000)


def validate_cron_expr(expr: str, tz: Optional[str] = None) -> Optional[str]:
    """校验 5 字段 cron；非法返回错误文案，合法返回 None。"""
    text = (expr or "").strip()
    if not text:
        return "cron_expr is empty"
    if len(text.split()) != 5:
        return f"Invalid cron expression '{text}'. Expected 5 fields: M H DoM Mon DoW."
    try:
        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo(tz) if tz else None
        start_dt = datetime.now(tz=tzinfo) if tzinfo else datetime.now()
        it = croniter(text, start_dt)
        it.get_next(datetime)
    except Exception as e:
        return f"Invalid cron expression '{text}': {e}"
    return None


def _next_run_ms(job: CronJob, now_ms: Optional[int] = None) -> Optional[int]:
    """计算该任务的下次执行时间（毫秒），无则返回 None。"""
    now_ms = now_ms or _now_ms()
    s = job.schedule
    if s.kind == "at":
        if s.at_ms is None:
            return None
        return s.at_ms if s.at_ms > now_ms else None
    if s.kind == "every":
        if s.every_ms is None or s.every_ms <= 0:
            return None
        last = job.state.last_run_at_ms
        base = last if last else now_ms
        return base + s.every_ms
    if s.kind == "cron":
        if not s.expr:
            return None
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(s.tz) if s.tz else None
            start_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=tz) if tz else datetime.fromtimestamp(now_ms / 1000.0)
            it = croniter(s.expr, start_dt)
            next_dt = it.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception as e:
            logging.warning("cron next_run for job %s: %s", job.id, e)
            return None
    return None


async def _get_next_wake_ms(store: CronStore) -> Optional[int]:
    """所有已启用任务中，最近一次下次执行时间。"""
    jobs = await store.list_jobs()
    now = _now_ms()
    times = []
    for j in jobs:
        if not j.enabled:
            continue
        n = j.state.next_run_at_ms or _next_run_ms(j, now)
        if n is not None:
            times.append(n)
    return min(times) if times else None


async def _on_tick(store: CronStore, execute: Callable[[CronJob], Awaitable[None]]) -> None:
    """到点执行：找出到期任务并执行，更新状态并写回 store。"""
    jobs = await store.list_jobs()
    now = _now_ms()
    due: List[CronJob] = []
    for j in jobs:
        if not j.enabled:
            continue
        n = j.state.next_run_at_ms or _next_run_ms(j, now)
        if n is not None and n <= now:
            due.append(j)
    for j in due:
        try:
            await execute(j)
            j.state.last_status = "ok"
            j.state.last_error = None
        except Exception as e:
            logging.exception("Execute job %s failed: %s", j.id, e)
            j.state.last_status = "error"
            j.state.last_error = str(e)
        j.state.last_run_at_ms = _now_ms()
        next_ms = _next_run_ms(j, j.state.last_run_at_ms)
        j.state.next_run_at_ms = next_ms
        if j.schedule.kind == "at" and next_ms is None:
            j.enabled = False
        await store.update_job(j)
        if getattr(j, "delete_after_run", False):
            await store.remove_job(j.id)


async def _run_loop(store: CronStore, execute: Callable[[CronJob], Awaitable[None]]) -> None:
    """循环：sleep 到最近下次执行时间（或最大轮询间隔）→ 执行到期任务 → 再 arm。"""
    while True:
        try:
            next_wake = await _get_next_wake_ms(store)
            now = _now_ms()
            if next_wake is not None:
                delay_sec = max(0.0, (next_wake - now) / 1000.0)
                delay_sec = min(delay_sec, POLL_INTERVAL_SEC)
            else:
                delay_sec = POLL_INTERVAL_SEC
            await asyncio.sleep(delay_sec)
            await _on_tick(store, execute)
        except asyncio.CancelledError:
            logging.info("Cron runner cancelled")
            break
        except Exception as e:
            logging.exception("Cron runner error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SEC)


class CronManager:
    """
    到期执行：通过 on_execute(job) 回调执行。
    预置实现见 executor.default_on_execute：按 CronKind.REMIND 推送通知、CronKind.AGENT 调用 Agent。
    """

    def __init__(
        self,
        store: Optional[CronStore] = None,
        on_execute: Optional[Callable[[CronJob], Awaitable[None]]] = None,
    ):
        self._store: CronStore = store or CronFileStore()
        self._on_execute = on_execute
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            logging.warning("Cron manager already running")
            return

        async def _execute(job: CronJob) -> None:
            if self._on_execute:
                await self._on_execute(job)

        self._task = asyncio.create_task(
            _run_loop(self._store, _execute),
            name="cron_runner",
        )
        logging.info("Cron manager started")

    async def stop(self) -> None:
        """停止并等待 Cron 后台任务退出。"""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._task is task:
                self._task = None
        logging.info("Cron manager stopped")

    async def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        payload: CronPayload,
        *,
        enabled: bool = True,
        delete_after_run: bool = False,
        durable: bool = True,
    ) -> CronJob:
        existing = await self._store.list_jobs()
        if len(existing) >= MAX_JOBS:
            raise ValueError(f"Too many scheduled jobs (max {MAX_JOBS}). Cancel one first.")
        if schedule.kind == "cron":
            err = validate_cron_expr(schedule.expr or "", schedule.tz)
            if err:
                raise ValueError(err)

        now_ms = _now_ms()
        job_id = str(uuid.uuid4())
        dummy = CronJob(
            id="",
            name="",
            enabled=enabled,
            schedule=schedule,
            payload=payload,
            state=CronJobState(next_run_at_ms=None, last_run_at_ms=None),
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            delete_after_run=delete_after_run,
            durable=durable,
        )

        job = CronJob(
            id=job_id,
            name=name,
            enabled=enabled,
            schedule=schedule,
            payload=payload,
            state=CronJobState(next_run_at_ms=_next_run_ms(dummy, now_ms)),
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
            delete_after_run=delete_after_run,
            durable=durable,
        )
        await self._store.add_job(job)
        logging.info(
            "Added cron job id=%s name=%s durable=%s",
            job_id,
            name,
            durable,
        )
        return job

    def _belongs_to_user(self, job: CronJob, user_id: Optional[str]) -> bool:
        if not user_id:
            return True
        return (job.payload.user_id or "") == user_id

    async def remove_job(self, job_id: str, user_id: Optional[str] = None) -> bool:
        job = await self.get_job(job_id, user_id)
        if job is None:
            return False
        await self._store.remove_job(job_id)
        logging.info("Removed cron job id=%s", job_id)
        return True

    async def list_jobs(self, user_id: Optional[str] = None) -> List[CronJob]:
        jobs = await self._store.list_jobs()
        if user_id is None:
            return jobs
        return [j for j in jobs if self._belongs_to_user(j, user_id)]

    async def get_job(self, job_id: str, user_id: Optional[str] = None) -> Optional[CronJob]:
        job = await self._store.get_job(job_id)
        if job is None or not self._belongs_to_user(job, user_id):
            return None
        return job

    async def set_job_enabled(self, job_id: str, enabled: bool, user_id: Optional[str] = None) -> bool:
        job = await self.get_job(job_id, user_id)
        if job is None:
            return False
        job.enabled = enabled
        job.updated_at_ms = _now_ms()
        await self._store.update_job(job)
        return True

    async def update_job(self, job: CronJob, user_id: Optional[str] = None) -> None:
        """将已修改的 job 写回存储（如修改 payload.message 后调用）。传入 user_id 时校验归属。"""
        if user_id is not None and not self._belongs_to_user(job, user_id):
            raise ValueError("job does not belong to user")
        job.updated_at_ms = _now_ms()
        await self._store.update_job(job)

    async def run_job_now(self, job_id: str, user_id: Optional[str] = None) -> bool:
        job = await self.get_job(job_id, user_id)
        if job is None:
            return False
        if self._on_execute:
            await self._on_execute(job)
        job.state.last_run_at_ms = _now_ms()
        job.state.next_run_at_ms = _next_run_ms(job, job.state.last_run_at_ms)
        await self._store.update_job(job)
        return True


CRON_MANAGER = CronManager(store=CronFileStore(), on_execute=default_on_execute)

def start_cron() -> None:
    """启动 Cron 管理器"""
    CRON_MANAGER.start()

async def stop_cron() -> None:
    """停止 Cron 管理器并等待退出。"""
    await CRON_MANAGER.stop()
