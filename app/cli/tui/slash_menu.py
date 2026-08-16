from __future__ import annotations

from typing import List, Optional
from textual.widgets import OptionList
from textual.widgets.option_list import Option
from app.cli.tui.slash_commands import SlashCommand, SlashCommandRegistry, SLASH_COMMANDS


class SlashCommandMenu(OptionList):
    """输入 `/` 时显示的斜杠命令提示列表。"""

    def __init__(self, registry: Optional[SlashCommandRegistry] = None) -> None:
        super().__init__(id="slash-menu", compact=True)
        self._registry = registry or SLASH_COMMANDS
        self._matches: List[SlashCommand] = []
        self.display = False
        self.can_focus = False

    @property
    def is_open(self) -> bool:
        return bool(self.display) and bool(self._matches)

    def highlighted_command(self) -> Optional[SlashCommand]:
        if not self.is_open:
            return None
        index = self.highlighted
        if index is None or index < 0 or index >= len(self._matches):
            return None
        return self._matches[index]

    def hide(self) -> None:
        self._matches = []
        self.clear_options()
        self.display = False

    def refresh_for_input(self, value: str) -> None:
        text = value or ""
        if not text.startswith("/"):
            self.hide()
            return
        # 已输入完整命令且带参数时，不再弹窗
        if " " in text.strip():
            self.hide()
            return
        matches = self._registry.match(text)
        if not matches:
            self.hide()
            return
        self._matches = matches
        self.clear_options()
        for cmd in matches:
            self.add_option(Option(cmd.label(), id=cmd.name))
        self.display = True
        self.highlighted = 0

    def move_highlight(self, delta: int) -> None:
        if not self.is_open:
            return
        count = len(self._matches)
        if count <= 0:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = (current + delta) % count
