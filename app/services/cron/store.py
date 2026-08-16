"""Cron 存储抽象与单机 JSON 文件实现。"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Protocol
from app.config.settings import settings
from .types import CronJob, CronJobState, CronKind, CronPayload, CronSchedule


class CronStore(Protocol):
    """存储抽象：统一 CRUD 接口，不区分文件/数据库等实现；涉及 I/O 的为异步。"""

    async def list_jobs(self) -> List[CronJob]:
        """获取所有任务（含 durable + session-only）。"""
        ...

    async def add_job(self, job: CronJob) -> None:
        """新增一条任务。"""
        ...

    async def get_job(self, job_id: str) -> Optional[CronJob]:
        """按 id 获取一条任务。"""
        ...

    async def update_job(self, job: CronJob) -> None:
        """更新指定任务。文件存储：读全量、替换该条、写回；数据库：UPDATE 单条。"""
        ...

    async def remove_job(self, job_id: str) -> bool:
        """删除指定任务，存在则删并返回 True，否则返回 False。"""
        ...


def _job_to_dict(j: CronJob) -> dict:
    return {
        "id": j.id,
        "name": j.name,
        "enabled": j.enabled,
        "schedule": {
            "kind": j.schedule.kind,
            "atMs": j.schedule.at_ms,
            "everyMs": j.schedule.every_ms,
            "expr": j.schedule.expr,
            "tz": j.schedule.tz,
        },
        "payload": {
            "kind": j.payload.kind.value,
            "message": j.payload.message,
            "triggerSessionId": j.payload.trigger_session_id,
            "needDeliver": j.payload.need_deliver,
            "userId": j.payload.user_id,
            "channelType": j.payload.channel_type,
            "channelId": j.payload.channel_id,
            "agentType": j.payload.agent_type,
            "extra": j.payload.extra,
        },
        "state": {
            "nextRunAtMs": j.state.next_run_at_ms,
            "lastRunAtMs": j.state.last_run_at_ms,
            "lastStatus": j.state.last_status,
            "lastError": j.state.last_error,
        },
        "createdAtMs": j.created_at_ms,
        "updatedAtMs": j.updated_at_ms,
        "deleteAfterRun": j.delete_after_run,
        "durable": j.durable,
    }


def _parse_payload_kind(k: str) -> CronKind:
    k = (k or "remind").lower()
    if k == CronKind.AGENT.value:
        return CronKind.AGENT
    return CronKind.REMIND


def _dict_to_job(d: dict) -> CronJob:
    s = d["schedule"]
    p = d["payload"]
    st = d.get("state") or {}
    return CronJob(
        id=d["id"],
        name=d["name"],
        enabled=d.get("enabled", True),
        schedule=CronSchedule(
            kind=s["kind"],
            at_ms=s.get("atMs"),
            every_ms=s.get("everyMs"),
            expr=s.get("expr"),
            tz=s.get("tz"),
        ),
        payload=CronPayload(
            kind=_parse_payload_kind(p.get("kind", "remind")),
            message=p.get("message", ""),
            trigger_session_id=p.get("triggerSessionId"),
            need_deliver=p.get("needDeliver", False),
            user_id=p.get("userId"),
            channel_type=p.get("channelType"),
            channel_id=p.get("channelId"),
            agent_type=p.get("agentType"),
            extra=p.get("extra") or {},
        ),
        state=CronJobState(
            next_run_at_ms=st.get("nextRunAtMs"),
            last_run_at_ms=st.get("lastRunAtMs"),
            last_status=st.get("lastStatus"),
            last_error=st.get("lastError"),
        ),
        created_at_ms=d.get("createdAtMs", 0),
        updated_at_ms=d.get("updatedAtMs", 0),
        delete_after_run=d.get("deleteAfterRun", False),
        durable=d.get("durable", True),
    )


class CronFileStore:
    """
    durable 任务写入 JSON；session-only（durable=False）仅内存，进程退出即丢。
    """

    def __init__(self):
        self._path = Path(settings.runtime_data_dir) / "cron" / "cron.json"
        self._cache: Optional[List[CronJob]] = None
        self._session: Dict[str, CronJob] = {}

    def _load(self) -> List[CronJob]:
        if not self._path.is_file():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            jobs = [_dict_to_job(j) for j in data.get("jobs", [])]
            for j in jobs:
                j.durable = True
            return jobs
        except Exception as e:
            logging.warning("Failed to load cron store %s: %s", self._path, e)
            return []

    def _save(self, jobs: List[CronJob]) -> None:
        durable = [j for j in jobs if j.durable]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "jobs": [_job_to_dict(j) for j in durable]}
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _ensure_cache(self) -> None:
        if self._cache is None:
            self._cache = await asyncio.to_thread(self._load)

    async def list_jobs(self) -> List[CronJob]:
        await self._ensure_cache()
        return list(self._cache or []) + list(self._session.values())

    async def get_job(self, job_id: str) -> Optional[CronJob]:
        await self._ensure_cache()
        if job_id in self._session:
            return self._session[job_id]
        for j in self._cache or []:
            if j.id == job_id:
                return j
        return None

    async def add_job(self, job: CronJob) -> None:
        await self._ensure_cache()
        if not job.durable:
            self._session[job.id] = job
            return
        jobs = await asyncio.to_thread(self._load)
        jobs.append(job)
        await asyncio.to_thread(self._save, jobs)
        self._cache = jobs

    async def update_job(self, job: CronJob) -> None:
        await self._ensure_cache()
        if job.id in self._session:
            if not job.durable:
                self._session[job.id] = job
                return
            del self._session[job.id]
            jobs = await asyncio.to_thread(self._load)
            jobs.append(job)
            await asyncio.to_thread(self._save, jobs)
            self._cache = jobs
            return
        jobs = await asyncio.to_thread(self._load)
        for i, j in enumerate(jobs):
            if j.id == job.id:
                if job.durable:
                    jobs[i] = job
                    await asyncio.to_thread(self._save, jobs)
                    self._cache = jobs
                else:
                    jobs = [x for x in jobs if x.id != job.id]
                    await asyncio.to_thread(self._save, jobs)
                    self._cache = jobs
                    self._session[job.id] = job
                return

    async def remove_job(self, job_id: str) -> bool:
        await self._ensure_cache()
        if job_id in self._session:
            del self._session[job_id]
            return True
        jobs = await asyncio.to_thread(self._load)
        before = len(jobs)
        jobs = [j for j in jobs if j.id != job_id]
        if len(jobs) == before:
            return False
        await asyncio.to_thread(self._save, jobs)
        self._cache = jobs
        return True
