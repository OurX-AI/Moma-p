"""Shell 命令包装剥离：匹配权限规则前去掉 timeout/env 等包装。"""
from __future__ import annotations
import re

_STRIP_PATTERNS = (
    re.compile(r"^(?:NODE_ENV|PYTHONPATH|PATH|HOME|LANG|LC_ALL|TZ|TERM|PWD)=[^\s]+\s+"),
    re.compile(r"^timeout\s+(?:-[^\s]+\s+)*\d+(?:\.\d+)?\s+"),
    re.compile(r"^time\s+"),
    re.compile(r"^nice\s+(?:-[^\s]+\s+)*"),
    re.compile(r"^nohup\s+"),
    re.compile(r"^stdbuf\s+(?:-[^\s]+\s+)+"),
    re.compile(r"^command\s+(?!-)"),
    re.compile(r"^builtin\s+"),
    re.compile(r"^noglob\s+"),
    re.compile(r"^xargs\s+(?!-)"),
)


class CommandWrapperStripper:
    """剥离固定包装，使 Bash(npm test *) 能匹配 `timeout 30 npm test`。"""

    @classmethod
    def strip(cls, command: str) -> str:
        text = (command or "").strip()
        if not text:
            return text
        for _ in range(16):
            progressed = False
            for pattern in _STRIP_PATTERNS:
                m = pattern.match(text)
                if not m:
                    continue
                rest = text[m.end() :].lstrip()
                if rest and rest != text:
                    text = rest
                    progressed = True
                    break
            if not progressed:
                break
        return text
