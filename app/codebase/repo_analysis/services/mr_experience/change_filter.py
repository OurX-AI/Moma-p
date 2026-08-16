from __future__ import annotations
import os
import re
from typing import List, Optional, Set
from .models import FileChange


class ChangeFilter:
    """规则筛选强相关变更文件：黑名单 + 改动量 Top-K。"""

    DEFAULT_TOP_K = 12
    EXCLUDED_DIR_NAMES: Set[str] = {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "__pycache__",
        ".git",
        ".idea",
        ".vscode",
        ".pytest_cache",
        ".mypy_cache",
    }
    EXCLUDED_FILE_NAMES: Set[str] = {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "go.sum",
        "cargo.lock",
    }
    EXCLUDED_SUFFIXES = (
        ".pyc",
        ".min.js",
        ".map",
        ".lock",
    )
    _MERGE_MSG = re.compile(r"^merge\b", re.I)
    _LOW_VALUE_NAMES = {
        "start.sh",
        "stop.sh",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
    }

    @classmethod
    def prefilter_skip_reason(cls, message: str, files: List[FileChange]) -> Optional[str]:
        """规则预筛：明显无可提炼价值的 MR，跳过 LLM。"""
        selected = cls.select(files)
        if not selected:
            return "无有效变更文件"
        msg = (message or "").strip()
        if not msg:
            return "缺少合入说明"
        paths = {f.path.replace("\\", "/").lower() for f in selected}
        if len(paths) == 1:
            base = os.path.basename(next(iter(paths))).lower()
            if base in cls._LOW_VALUE_NAMES:
                return "仅运维/依赖锁文件变更，无可复用开发经验"
        if cls._MERGE_MSG.match(msg) and len(selected) <= 2:
            if all(os.path.basename(p).lower() in cls._LOW_VALUE_NAMES for p in paths):
                return "纯同步合并，无可复用开发经验"
        return None

    @classmethod
    def select(cls, files: List[FileChange], top_k: int = DEFAULT_TOP_K) -> List[FileChange]:
        kept = [f for f in files if f.path and not cls.should_exclude(f.path)]
        kept.sort(key=lambda f: (f.churn, f.additions + f.deletions), reverse=True)
        return kept[: max(1, top_k)] if kept else []

    @classmethod
    def should_exclude(cls, path: str) -> bool:
        norm = (path or "").replace("\\", "/").strip("/")
        if not norm:
            return True
        base = os.path.basename(norm).lower()
        if base in cls.EXCLUDED_FILE_NAMES:
            return True
        lower = norm.lower()
        for suf in cls.EXCLUDED_SUFFIXES:
            if lower.endswith(suf):
                return True
        parts = lower.split("/")
        if any(p in cls.EXCLUDED_DIR_NAMES for p in parts):
            return True
        return False

    @staticmethod
    def status_action(status: str) -> str:
        st = (status or "M")[:1].upper()
        if st == "A":
            return "新增"
        if st == "D":
            return "删除"
        if st == "R":
            return "重命名/调整"
        return "修改"
