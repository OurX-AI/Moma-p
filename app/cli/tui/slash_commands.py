from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    usage: str = ""

    @property
    def trigger(self) -> str:
        return f"/{self.name}"

    def label(self) -> str:
        usage = f"  {self.usage}" if self.usage else ""
        return f"{self.trigger:<12} {self.summary}{usage}"


class SlashCommandRegistry:
    """CLI / TUI 斜杠命令注册表。"""

    def __init__(self, commands: Iterable[SlashCommand]) -> None:
        self._commands = list(commands)

    @classmethod
    def default(cls) -> "SlashCommandRegistry":
        return cls(
            [
                SlashCommand("help", "显示帮助与全部斜杠命令"),
                SlashCommand("commands", "列出可用斜杠命令（同 /help）"),
                SlashCommand("new", "开启新会话"),
                SlashCommand("clear", "清空当前对话显示"),
                SlashCommand("resume", "查看并切换历史会话", "[session_id]"),
                SlashCommand("continue", "切换历史会话（同 /resume）"),
                SlashCommand("status", "查看 Agent / 会话状态"),
                SlashCommand("codebase", "CodeBase 子命令", "status|rescan|clean|locate|query|similar|experience"),
                SlashCommand("model", "查看或设置模型", "provider/model"),
                SlashCommand("stop", "软中断当前任务"),
                SlashCommand("kill", "强制终止当前任务"),
                SlashCommand("queue", "查看或清空排队消息", "[clear]"),
                SlashCommand("exit", "退出 MOMA"),
                SlashCommand("quit", "退出 MOMA（同 /exit）"),
            ]
        )

    def all(self) -> List[SlashCommand]:
        return list(self._commands)

    def help_markdown(self) -> str:
        lines = ["## Slash commands", ""]
        for cmd in self._commands:
            usage = f" `{cmd.usage}`" if cmd.usage else ""
            lines.append(f"- `/{cmd.name}`{usage} — {cmd.summary}")
        lines.extend(
            [
                "",
                "直接输入自然语言任务即可开始对话。",
                "输入 `/` 可弹出命令提示；↑↓ 选择，Enter 执行，Esc 关闭。",
                "`/resume` 打开历史会话列表；↑↓ 选择，Enter 切换，Esc 关闭。",
            ]
        )
        return "\n".join(lines)

    def match(self, query: str) -> List[SlashCommand]:
        raw = (query or "").strip()
        if not raw.startswith("/"):
            return []
        token = raw.split(maxsplit=1)[0]
        key = token[1:].casefold()
        if key == "":
            return self.all()
        exact: List[SlashCommand] = []
        prefix: List[SlashCommand] = []
        contains: List[SlashCommand] = []
        for cmd in self._commands:
            name = cmd.name.casefold()
            if name == key:
                exact.append(cmd)
            elif name.startswith(key):
                prefix.append(cmd)
            elif key in name or key in cmd.summary.casefold():
                contains.append(cmd)
        return exact + prefix + contains

    def resolve(self, line: str) -> Optional[SlashCommand]:
        raw = (line or "").strip()
        if not raw.startswith("/"):
            return None
        token = raw.split(maxsplit=1)[0][1:].casefold()
        for cmd in self._commands:
            if cmd.name.casefold() == token:
                return cmd
        return None


SLASH_COMMANDS = SlashCommandRegistry.default()
