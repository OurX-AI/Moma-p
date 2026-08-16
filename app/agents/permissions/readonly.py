"""内置只读命令识别（readonly_or_deny 模式用）。"""
from __future__ import annotations
import re
from .rule import CommandSplitter
from .wrappers import CommandWrapperStripper

# 常见只读探测命令（可被 deny 规则覆盖）
_READONLY_PREFIXES = (
    "ls",
    "dir",
    "cat",
    "type",
    "echo",
    "pwd",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "wc",
    "which",
    "where",
    "whereis",
    "diff",
    "stat",
    "du",
    "df",
    "file",
    "realpath",
    "readlink",
    "basename",
    "dirname",
    "cd",
    "test",
    "[",
    "true",
    "false",
    "printf",
    "env",
    "printenv",
    "uname",
    "whoami",
    "id",
    "date",
    "cal",
    "hostname",
    "getent",
    "locale",
    "tree",
    "less",
    "more",
    "nl",
    "od",
    "hexdump",
    "jq",
    "yq",
    "awk",
    "sed",
    "sort",
    "uniq",
    "cut",
    "tr",
    "column",
    "comm",
    "cmp",
    "md5sum",
    "sha256sum",
    "sha1sum",
    "Get-ChildItem",
    "Get-Content",
    "Get-Location",
    "Get-Item",
    "Get-Command",
    "Select-String",
    "Test-Path",
    "Resolve-Path",
    "Write-Output",
    "Write-Host",
)

_READONLY_GIT = re.compile(
    r"^git\s+(status|diff|log|show|branch|tag|remote|rev-parse|describe|ls-files|blame|ls-tree|cat-file|config\s+--get)(\s|$)",
    re.IGNORECASE,
)

_READONLY_PREFIX_SET = {p.lower() for p in _READONLY_PREFIXES}


class ReadonlyCommandClassifier:
    """判断 Bash/PowerShell 命令是否为内置只读。"""

    @classmethod
    def is_readonly_command(cls, command: str) -> bool:
        parts = CommandSplitter.split(command)
        if not parts:
            return False
        return all(cls._is_readonly_segment(p) for p in parts)

    @classmethod
    def _is_readonly_segment(cls, segment: str) -> bool:
        s = CommandWrapperStripper.strip(segment or "")
        if not s:
            return False
        # 去掉简单环境变量前缀 FOO=bar
        s = re.sub(r"^(?:[A-Za-z_][\w]*=\S+\s+)+", "", s)
        lower = s.lower()
        if _READONLY_GIT.match(lower):
            return True
        first = lower.split(None, 1)[0] if lower else ""
        first = first.strip("\"'")
        if first not in _READONLY_PREFIX_SET:
            return False
        # sed -i 会原地改写，不算只读
        if first == "sed" and re.search(r"(?:^|\s)-i(?:\s|$)", s):
            return False
        return True
