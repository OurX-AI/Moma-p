from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence
from textual.widgets import OptionList
from textual.widgets.option_list import Option


class SessionPicker(OptionList):
    """/resume 会话选择器：↑↓ 选择，Enter 切换，Esc 关闭。"""

    def __init__(self) -> None:
        super().__init__(id="session-picker", compact=True)
        self._session_ids: List[str] = []
        self._ignore_select = False
        self.display = False
        self.can_focus = False

    @property
    def is_open(self) -> bool:
        return bool(self.display) and bool(self._session_ids)

    @property
    def ignore_select(self) -> bool:
        return self._ignore_select

    def highlighted_session_id(self) -> Optional[str]:
        if not self.is_open:
            return None
        index = self.highlighted
        if index is None or index < 0 or index >= len(self._session_ids):
            return None
        return self._session_ids[index]

    def session_id_at(self, option_id: str | None) -> Optional[str]:
        if not option_id or not option_id.startswith("resume-"):
            return None
        try:
            index = int(option_id.split("-", 1)[1])
        except ValueError:
            return None
        if index < 0 or index >= len(self._session_ids):
            return None
        return self._session_ids[index]

    def hide(self) -> None:
        self._session_ids = []
        self._ignore_select = False
        self.clear_options()
        self.display = False

    def show_sessions(
        self,
        sessions: Sequence[object],
        *,
        current_session_id: str,
    ) -> None:
        self._ignore_select = True
        self.clear_options()
        self._session_ids = []
        for index, session in enumerate(sessions):
            sid = getattr(session, "session_id", "") or ""
            if not sid:
                continue
            self._session_ids.append(sid)
            self.add_option(
                Option(self._label(session, current_session_id), id=f"resume-{index}")
            )
        if not self._session_ids:
            self.hide()
            return
        self.display = True
        try:
            self.highlighted = self._session_ids.index(current_session_id)
        except ValueError:
            self.highlighted = 0
        # 避免 OptionList 在填充/高亮时误发 OptionSelected 立刻关掉弹层
        self.set_timer(0.35, self._enable_select)

    def _enable_select(self) -> None:
        self._ignore_select = False

    def move_highlight(self, delta: int) -> None:
        if not self.is_open:
            return
        self._ignore_select = False
        count = len(self._session_ids)
        if count <= 0:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = (current + delta) % count

    @staticmethod
    def _label(session: object, current_session_id: str) -> str:
        sid = getattr(session, "session_id", "") or ""
        short_id = sid.split("_")[-1][:8] if sid else "?"
        stamp = ""
        last_updated = getattr(session, "last_updated", None)
        if isinstance(last_updated, datetime):
            stamp = last_updated.strftime("%m-%d %H:%M")
        title_fn = getattr(session, "display_title", None)
        if callable(title_fn):
            desc = title_fn(42)
        else:
            desc = (getattr(session, "description", None) or "").strip() or "Untitled"
            if len(desc) > 42:
                desc = desc[:41] + "..."
        marker = "> " if sid == current_session_id else "  "
        left = f"{stamp}  {desc}" if stamp else desc
        return f"{marker}{left:<52} {short_id}"
