from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional, Sequence, TypeVar

T = TypeVar("T")

DEFAULT_HEAD_LIMIT = 100
SAFETY_MAX_RESULTS = 10000

_TYPE_TO_GLOB = {
    "py": "*.py",
    "python": "*.py",
    "js": "*.{js,jsx}",
    "ts": "*.{ts,tsx}",
    "tsx": "*.tsx",
    "jsx": "*.jsx",
    "rust": "*.rs",
    "go": "*.go",
    "java": "*.java",
    "c": "*.{c,h}",
    "cpp": "*.{cpp,cc,cxx,hpp,h}",
    "cs": "*.cs",
    "rb": "*.rb",
    "php": "*.php",
    "md": "*.md",
    "json": "*.json",
    "yaml": "*.{yaml,yml}",
    "yml": "*.{yaml,yml}",
    "html": "*.{html,htm}",
    "css": "*.css",
    "sh": "*.{sh,bash,zsh}",
}


@dataclass(frozen=True)
class GrepQuery:
    """一次 grep 查询的参数封装。"""

    pattern: str
    include: Optional[str] = None
    file_type: Optional[str] = None
    case_insensitive: bool = False
    context: Optional[int] = None
    context_before: Optional[int] = None
    context_after: Optional[int] = None
    head_limit: int = DEFAULT_HEAD_LIMIT
    offset: int = 0
    multiline: bool = False

    def compile_regex(self) -> re.Pattern[str]:
        flags = 0
        if self.case_insensitive:
            flags |= re.IGNORECASE
        if self.multiline:
            flags |= re.DOTALL | re.MULTILINE
        return re.compile(self.pattern, flags)

    def resolved_include(self) -> Optional[str]:
        if self.include:
            return self.include
        key = (self.file_type or "").strip().lower()
        if not key:
            return None
        return _TYPE_TO_GLOB.get(key)

    def context_flags(self) -> tuple[Optional[int], Optional[int], Optional[int]]:
        """返回 (C, B, A)；C 优先于 B/A。"""
        if self.context is not None and self.context >= 0:
            return self.context, None, None
        before = self.context_before if self.context_before is not None and self.context_before >= 0 else None
        after = self.context_after if self.context_after is not None and self.context_after >= 0 else None
        return None, before, after

    def wants_context(self) -> bool:
        c, b, a = self.context_flags()
        return bool((c and c > 0) or (b and b > 0) or (a and a > 0))


class GrepResultWindow:
    """对结果列表应用 offset / head_limit。"""

    @staticmethod
    def apply(items: Sequence[T], *, offset: int, head_limit: int) -> tuple[list[T], int, int]:
        """返回 (窗口内条目, 原始总数, 实际使用的 limit)。"""
        total = len(items)
        start = max(0, int(offset or 0))
        if head_limit == 0:
            limit = SAFETY_MAX_RESULTS
        else:
            limit = max(0, int(head_limit))
        window = list(items[start : start + limit])
        return window, total, limit
