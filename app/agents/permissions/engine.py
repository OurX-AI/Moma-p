"""权限裁决引擎：硬拒绝 → deny → allow → mode.unmatched。"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from .config import PermissionConfig
from .readonly import ReadonlyCommandClassifier
from .rule import CommandSplitter
from .schemes import UnmatchedPolicy
from .wrappers import CommandWrapperStripper


TOOL_ALIASES = {
    "bash": "Bash",
    "powershell": "PowerShell",
    "read_file": "Read",
    "read_dir": "Read",
    "lsp": "Read",
    "write_file": "Write",
    "apply_patch": "Edit",
    "edit_file": "Edit",
    "grep_search": "Grep",
    "glob_search": "Glob",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
}

PRIMARY_INPUT_KEYS: dict[str, str] = {
    "bash": "command",
    "powershell": "command",
    "read_file": "path",
    "read_dir": "path",
    "lsp": "filePath",
    "write_file": "path",
    "edit_file": "path",
    "apply_patch": "patchText",
    "grep_search": "pattern",
    "glob_search": "pattern",
    "web_fetch": "url",
    "web_search": "query",
}

_HARD_SHELL_DENY = (
    re.compile(r"(?:^|[;&|]\s*)rm\s+-[a-zA-Z]*f[a-zA-Z]*\s+/(?:\s|$)", re.I),
    re.compile(r"(?:^|[;&|]\s*)rm\s+-[a-zA-Z]*f[a-zA-Z]*\s+~(?:/|\s|$)", re.I),
    re.compile(r"(?:^|[;&|]\s*)format(\.com|\.exe)?(\s|/|$)", re.I),
    re.compile(r"(?:^|[;&|]\s*)(shutdown|reboot|poweroff)\b", re.I),
    re.compile(r"(?:^|[;&|]\s*)(mkfs|diskpart)\b", re.I),
    re.compile(r"(?:^|[;&|]\s*)Remove-Item\b.*\s(-Recurse|-Force).*\b(C:\\\\|/)\b", re.I),
)

_DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)
_PATCH_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str
    matched_rule: str | None = None


class PermissionEngine:
    """对单次工具调用做 allow/deny 裁决（无 ask）。"""

    def __init__(self, config: PermissionConfig) -> None:
        self._config = config

    @classmethod
    def from_agent_config(cls, agent_config: dict | None) -> PermissionEngine:
        return cls(PermissionConfig.from_agent_config(agent_config))

    @property
    def config(self) -> PermissionConfig:
        return self._config

    @classmethod
    def primary_input(cls, tool_name: str, params: dict[str, Any] | None) -> str:
        params = params or {}
        key = PRIMARY_INPUT_KEYS.get((tool_name or "").strip())
        if not key:
            return ""
        value = params.get(key)
        return "" if value is None else str(value)

    @classmethod
    def paths_from_params(cls, tool_name: str, params: dict[str, Any] | None) -> list[str]:
        """提取用于 Read deny 联动 Edit/Write 的路径列表。"""
        params = params or {}
        name = (tool_name or "").strip()
        if name in {
            "read_file",
            "read_dir",
            "edit_file",
            "write_file",
        }:
            path = str(params.get("path") or "").strip()
            return [path] if path else []
        if name == "apply_patch":
            return cls._paths_from_patch(str(params.get("patchText") or ""))
        return []

    @staticmethod
    def _paths_from_patch(patch_text: str) -> list[str]:
        found: list[str] = []
        for m in _DIFF_PATH_RE.finditer(patch_text or ""):
            found.append(m.group(2).strip())
        for m in _PATCH_PATH_RE.finditer(patch_text or ""):
            found.append(m.group(1).strip())
        # 去重保序
        out: list[str] = []
        seen: set[str] = set()
        for p in found:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def decide(self, tool_name: str, primary_input: str) -> PermissionDecision:
        canonical = TOOL_ALIASES.get((tool_name or "").strip(), (tool_name or "").strip())
        if canonical in ("Bash", "PowerShell"):
            hard = self._hard_deny_shell(primary_input or "")
            if hard is not None:
                return hard
            if not self._config.enabled:
                return PermissionDecision(True, "permissions disabled")
            return self._decide_shell(canonical, primary_input or "")

        if not self._config.enabled:
            return PermissionDecision(True, "permissions disabled")
        return self._decide_simple(canonical, primary_input or "")

    def decide_tool_call(
        self, tool_name: str, params: dict[str, Any] | None
    ) -> PermissionDecision:
        canonical = TOOL_ALIASES.get((tool_name or "").strip(), (tool_name or "").strip())
        # Read deny 同时挡住同路径 Edit/Write
        if canonical in ("Edit", "Write"):
            for path in self.paths_from_params(tool_name, params):
                blocked = self._denied_by_read_rule(path)
                if blocked is not None:
                    return blocked
        return self.decide(tool_name, self.primary_input(tool_name, params))

    def _denied_by_read_rule(self, path: str) -> PermissionDecision | None:
        norm = self._normalize_path(path)
        for rule in self._config.deny:
            if not rule.matches_tool("Read"):
                continue
            if rule.specifier is None:
                return PermissionDecision(
                    False,
                    f"edit/write blocked by Read deny {rule.raw!r}",
                    matched_rule=rule.raw,
                )
            if rule.matches("Read", path) or rule.matches("Read", norm):
                return PermissionDecision(
                    False,
                    f"edit/write blocked by Read deny {rule.raw!r}",
                    matched_rule=rule.raw,
                )
        return None

    @staticmethod
    def _normalize_path(path: str) -> str:
        text = (path or "").replace("\\", "/").strip()
        while text.startswith("./"):
            text = text[2:]
        try:
            return PurePosixPath(text).as_posix()
        except Exception:
            return text

    def _hard_deny_shell(self, command: str) -> PermissionDecision | None:
        segments = CommandSplitter.split(command) or [command]
        for seg in segments:
            stripped = CommandWrapperStripper.strip(seg.strip())
            for pattern in _HARD_SHELL_DENY:
                if pattern.search(stripped):
                    return PermissionDecision(
                        False,
                        "denied by hard safety rule",
                        matched_rule=pattern.pattern,
                    )
        return None

    def _decide_shell(self, tool: str, command: str) -> PermissionDecision:
        segments = CommandSplitter.split(command) or [command]
        for seg in segments:
            stripped = CommandWrapperStripper.strip(seg)
            decision = self._decide_simple(tool, stripped)
            if not decision.allowed:
                return decision
        return PermissionDecision(True, "all shell segments allowed")

    def _decide_simple(self, tool: str, primary_input: str) -> PermissionDecision:
        for rule in self._config.deny:
            if rule.matches(tool, primary_input):
                return PermissionDecision(
                    False,
                    f"denied by rule {rule.raw!r}",
                    matched_rule=rule.raw,
                )
        for rule in self._config.allow:
            if rule.matches(tool, primary_input):
                return PermissionDecision(
                    True,
                    f"allowed by rule {rule.raw!r}",
                    matched_rule=rule.raw,
                )

        mode = self._config.resolve_mode()
        unmatched = mode.unmatched
        if unmatched is UnmatchedPolicy.ALLOW:
            return PermissionDecision(True, f"unmatched under mode {mode.name!r}: allow")
        if unmatched is UnmatchedPolicy.DENY:
            return PermissionDecision(False, f"unmatched under mode {mode.name!r}: deny")
        if tool in ("Bash", "PowerShell") and ReadonlyCommandClassifier.is_readonly_command(
            primary_input
        ):
            return PermissionDecision(
                True, f"unmatched under mode {mode.name!r}: builtin readonly"
            )
        return PermissionDecision(
            False, f"unmatched under mode {mode.name!r}: deny (not readonly)"
        )
