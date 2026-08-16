from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional
import time


class SubAgentTaskStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


SUBAGENT_DONE_MARKER = "[SUBAGENT_DONE]"

_SUBAGENT_FINALIZE_PROMPT = (
    "Background subagent tasks have finished. Their results are in the conversation history "
    f"(messages marked {SUBAGENT_DONE_MARKER}). "
    "Synthesize them into your final answer for the user. "
    "Do not spawn new subagents unless strictly necessary."
)


@dataclass
class SubAgentTaskRecord:
    """异步/同步 spawn 任务登记项。"""

    task_id: str
    label: str
    subagent_type: str
    status: SubAgentTaskStatus
    started_at: float
    mode: str = "async"
    task: str = ""
    result: Optional[str] = None
    finished_at: Optional[float] = None
    delivered: bool = False

    def to_summary(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "label": self.label,
            "type": self.subagent_type,
            "status": self.status.value,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "has_result": bool((self.result or "").strip()),
            "delivered": self.delivered,
        }

    def to_detail(self) -> Dict[str, Any]:
        data = self.to_summary()
        data["task"] = self.task
        data["result"] = self.result
        return data

    def history_notice(self) -> str:
        body = (self.result or "").strip() or "(empty result)"
        return SubAgentTaskRegistry.format_completion_notice(self, body)


class SubAgentTaskRegistry:
    """SubAgent 任务登记表（按 task_id 索引）。"""

    FINALIZE_PROMPT = _SUBAGENT_FINALIZE_PROMPT

    def __init__(self) -> None:
        self._records: Dict[str, SubAgentTaskRecord] = {}

    def register(
        self,
        *,
        task_id: str,
        label: str,
        subagent_type: str,
        mode: str,
        task: str = "",
    ) -> SubAgentTaskRecord:
        record = SubAgentTaskRecord(
            task_id=task_id,
            label=label,
            subagent_type=subagent_type,
            status=SubAgentTaskStatus.RUNNING,
            started_at=time.time(),
            mode=mode,
            task=task,
        )
        self._records[task_id] = record
        return record

    def get(self, task_id: str) -> Optional[SubAgentTaskRecord]:
        key = (task_id or "").strip()
        if not key:
            return None
        return self._records.get(key)

    def list(
        self,
        *,
        status: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[SubAgentTaskRecord]:
        items = list(self._records.values())
        if status:
            want = str(status).strip().lower()
            items = [r for r in items if r.status.value == want]
        if mode:
            want_mode = str(mode).strip().lower()
            items = [r for r in items if r.mode == want_mode]
        items.sort(key=lambda r: r.started_at, reverse=True)
        return items

    def mark_finished(
        self,
        task_id: str,
        *,
        status: SubAgentTaskStatus,
        result: Optional[str] = None,
        delivered: bool = False,
    ) -> Optional[SubAgentTaskRecord]:
        record = self.get(task_id)
        if record is None:
            return None
        if status == SubAgentTaskStatus.RUNNING:
            raise ValueError("finished status must not be running")
        record.status = status
        record.result = result
        record.finished_at = time.time()
        record.delivered = delivered
        return record

    def mark_cancelled_running(self) -> List[SubAgentTaskRecord]:
        """将仍为 running 的任务标为 cancelled（配合 cancel_tasks）。"""
        updated: List[SubAgentTaskRecord] = []
        now = time.time()
        for record in self._records.values():
            if record.status == SubAgentTaskStatus.RUNNING:
                record.status = SubAgentTaskStatus.CANCELLED
                record.finished_at = now
                if not record.result:
                    record.result = "Subagent cancelled."
                updated.append(record)
        return updated

    def has_running(self, *, mode: Optional[str] = "async") -> bool:
        for record in self._records.values():
            if record.status != SubAgentTaskStatus.RUNNING:
                continue
            if mode is not None and record.mode != mode:
                continue
            return True
        return False

    def take_undelivered_finished(self, *, mode: Optional[str] = "async") -> List[SubAgentTaskRecord]:
        """取出已结束且尚未写入主 History 的任务，并标记 delivered。"""
        out: List[SubAgentTaskRecord] = []
        for record in self._records.values():
            if record.status == SubAgentTaskStatus.RUNNING:
                continue
            if record.delivered:
                continue
            if mode is not None and record.mode != mode:
                continue
            record.delivered = True
            out.append(record)
        out.sort(key=lambda r: r.finished_at or r.started_at)
        return out

    @staticmethod
    def format_completion_notice(record: SubAgentTaskRecord, body: str) -> str:
        """写入主 Agent History 的结构化通知。"""
        text = (body or "").strip() or "(empty result)"
        return (
            f"{SUBAGENT_DONE_MARKER}\n"
            f"task_id: {record.task_id}\n"
            f"label: {record.label}\n"
            f"type: {record.subagent_type}\n"
            f"status: {record.status.value}\n"
            f"\n{text}"
        )

    @staticmethod
    def format_card_text(record: SubAgentTaskRecord) -> str:
        """TUI 子任务完成卡片短文案。"""
        return (
            f"Subagent [{record.label}] {record.status.value} "
            f"(id: {record.task_id}, type: {record.subagent_type})"
        )
