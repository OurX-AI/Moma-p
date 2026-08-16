from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Literal, Optional
from app.agents.output import OutboundMessage, OutboundMessageType
from app.cli.display_format import extract_tool_call_id, format_outbound_content

if TYPE_CHECKING:
    from app.cli.tui.app import MomaCoderApp

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"</think\s*>", re.IGNORECASE)


@dataclass
class PresenterAction:
    kind: Literal["none", "live", "commit", "tool", "ask", "plain", "subagent"]
    text: str = ""
    finished: bool = False
    success: Optional[bool] = None  # tool_result 的成功/失败标志，供 UI 上色
    tool_call_id: Optional[str | list[str]] = None  # tool_call 可能含多个 id，tool_result 为单个 id


class TuiOutputHandler:
    def __init__(self, app: MomaCoderApp) -> None:
        self._app = app

    async def __call__(self, msg: OutboundMessage) -> None:
        self._app.call_later(self._app.handle_agent_output, msg)


class OutputPresenter:
    """解析 Agent 输出，返回需由 App 执行的 UI 动作列表（Markdown 由 Textual Markdown 渲染）。

    返回 List：当工具结果/ask 等到达时，若流式缓冲区有未提交的模型 Content，
    会先 commit 出去（带 MOMA 标签），再追加工具行--保证 Content 在工具行上方。
    """

    def __init__(self) -> None:
        self._stream_open = False
        self._stream_buf = ""
        self._committed = False

    def feed(self, msg: OutboundMessage) -> List[PresenterAction]:
        outbound_type = msg.outbound_type
        if outbound_type == OutboundMessageType.STREAM_START:
            if self._stream_open:
                return []
            self._stream_open = True
            self._stream_buf = ""
            self._committed = False
            return [PresenterAction("live", "...")]
        if outbound_type == OutboundMessageType.STREAM_DELTA:
            if not msg.content:
                return []
            if not self._stream_open:
                self._stream_open = True
                self._stream_buf = ""
                self._committed = False
            self._stream_buf += msg.content
            visible = self.strip_think_blocks(self._stream_buf, drop_open=True).strip()
            return [PresenterAction("live", visible or "...")]
        if outbound_type == OutboundMessageType.STREAM_END:
            action = self._commit_action(finished=False)
            return [action] if action.kind == "commit" else []
        if outbound_type == OutboundMessageType.RUN_END:
            action = self._commit_action(finished=True)
            action.finished = True
            return [action] if action.kind == "commit" else [PresenterAction("none", finished=True)]
        if outbound_type == OutboundMessageType.SUBAGENT_DONE:
            text = self.strip_think_blocks(msg.content or "").strip()
            if not text:
                meta = msg.metadata or {}
                tid = str(meta.get("task_id") or "").strip()
                label = str(meta.get("label") or "").strip() or "subagent"
                status = str(meta.get("status") or "completed").strip()
                text = f"Subagent [{label}] {status}" + (f" (id: {tid})" if tid else "")
            return self._with_pending_commit([PresenterAction("subagent", text)])
        if msg.content:
            text = self.strip_think_blocks(msg.content).strip()
            if not text:
                return []
            kind, formatted = format_outbound_content(text)
            if kind == "skip" or not formatted:
                return []
            if kind == "tool":
                success = self._extract_tool_success(text)
                tool_call_id = extract_tool_call_id(text)
                return self._with_pending_commit(
                    [PresenterAction("tool", formatted, success=success, tool_call_id=tool_call_id)]
                )
            if kind == "ask":
                return self._with_pending_commit([PresenterAction("ask", formatted)])
            if formatted.startswith("Tool "):
                return self._with_pending_commit([PresenterAction("tool", formatted)])
            return self._with_pending_commit([PresenterAction("plain", formatted)])
        return []

    def _with_pending_commit(
        self, actions: List[PresenterAction]
    ) -> List[PresenterAction]:
        """工具/ask/plain/subagent 到达时，若流式缓冲区有未提交的模型 Content，先 commit。"""
        commit = self._commit_action(finished=False)
        if commit.kind == "commit":
            return [commit] + actions
        return actions

    @staticmethod
    def _extract_tool_success(text: str) -> Optional[bool]:
        """从 tool_result JSON 里取 success 字段；tool_call 无此字段返回 None。"""
        raw = (text or "").strip()
        if not raw.startswith("{"):
            return None
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        if obj.get("kind") != "tool_result":
            return None
        success = obj.get("success")
        return bool(success) if isinstance(success, bool) else None

    def _commit_action(self, *, finished: bool) -> PresenterAction:
        if self._committed:
            return PresenterAction("none", finished=finished)
        body = self.strip_think_blocks(self._stream_buf).strip()
        self._stream_buf = ""
        self._stream_open = False
        self._committed = True
        if body:
            return PresenterAction("commit", body, finished=finished)
        return PresenterAction("none", finished=finished)

    @staticmethod
    def strip_think_blocks(text: str, *, drop_open: bool = False) -> str:
        cleaned = _THINK_BLOCK_RE.sub("", text or "")
        if drop_open:
            cleaned = _THINK_OPEN_RE.sub("", cleaned)
        cleaned = _THINK_CLOSE_RE.sub("", cleaned)
        return cleaned.strip()
