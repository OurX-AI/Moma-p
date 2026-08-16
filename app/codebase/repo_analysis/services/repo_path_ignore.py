from __future__ import annotations
import fnmatch
import os
from dataclasses import dataclass, field
from typing import List, Optional, Set
from app.utils.common import normalize_path


_DEFAULT_BUILTIN_DIRS: Set[str] = {
    "__pycache__",
    ".git",
    ".idea",
    ".vscode",
    "venv",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "target",
    ".pytest_cache",
    ".mypy_cache",
    ".coverage",
    "__tests__",
    "tests",
}


@dataclass
class RepoPathIgnore:
    """仓库路径忽略：内置排除 + .gitignore + .momaignore。"""

    repo_root: str
    builtin_dir_names: Set[str] = field(default_factory=lambda: set(_DEFAULT_BUILTIN_DIRS))
    patterns: List[str] = field(default_factory=list)
    gitignore_loaded: bool = False
    momaignore_loaded: bool = False

    @classmethod
    def load(
        cls,
        repo_root: str,
        *,
        builtin_dir_names: Optional[Set[str]] = None,
    ) -> "RepoPathIgnore":
        root = os.path.abspath(os.path.normpath(repo_root))
        builtin = set(builtin_dir_names if builtin_dir_names is not None else _DEFAULT_BUILTIN_DIRS)
        patterns: List[str] = []
        gitignore_loaded = False
        momaignore_loaded = False

        gitignore = os.path.join(root, ".gitignore")
        if os.path.isfile(gitignore):
            patterns.extend(cls._read_patterns(gitignore))
            gitignore_loaded = True

        momaignore = os.path.join(root, ".momaignore")
        if os.path.isfile(momaignore):
            patterns.extend(cls._read_patterns(momaignore))
            momaignore_loaded = True

        return cls(
            repo_root=root,
            builtin_dir_names=builtin,
            patterns=patterns,
            gitignore_loaded=gitignore_loaded,
            momaignore_loaded=momaignore_loaded,
        )

    @staticmethod
    def _read_patterns(path: str) -> List[str]:
        out: List[str] = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    out.append(line)
        except OSError:
            return []
        return out

    def describe(self) -> dict:
        return {
            "sources": ["builtin", ".gitignore", ".momaignore"],
            "builtin_dir_count": len(self.builtin_dir_names),
            "pattern_count": len(self.patterns),
            "gitignore_loaded": self.gitignore_loaded,
            "momaignore_loaded": self.momaignore_loaded,
        }

    def should_prune_dir(self, rel_dir: str, dir_name: str) -> bool:
        """walk 时是否剪掉该子目录。"""
        name = str(dir_name or "")
        if not name:
            return True
        if name.startswith("."):
            return True
        if name in self.builtin_dir_names:
            return True
        rel = self._norm_rel(rel_dir)
        return self._is_ignored(rel, is_dir=True)

    def should_ignore_file(self, rel_file: str) -> bool:
        rel = self._norm_rel(rel_file)
        if not rel:
            return True
        parts = rel.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.startswith(".") or part in self.builtin_dir_names:
                return True
            parent = "/".join(parts[: i + 1])
            if self._is_ignored(parent, is_dir=True):
                return True
        return self._is_ignored(rel, is_dir=False)

    def filter_walk_dirs(self, parent_root: str, dirs: List[str]) -> List[str]:
        """就地过滤 dirs，返回被剪枝目录的相对路径列表。"""
        pruned: List[str] = []
        excluded: List[str] = []
        for d in list(dirs):
            sub_abs = os.path.join(parent_root, d)
            rel_sub = normalize_path(os.path.relpath(sub_abs, self.repo_root))
            if rel_sub == ".":
                rel_sub = d
            if self.should_prune_dir(rel_sub, d):
                excluded.append(rel_sub if rel_sub != "." else d)
            else:
                pruned.append(d)
        dirs[:] = pruned
        return excluded

    @staticmethod
    def _norm_rel(path: str) -> str:
        return normalize_path(str(path or "")).strip("/")

    def _is_ignored(self, rel: str, *, is_dir: bool) -> bool:
        if not rel:
            return False
        ignored = False
        for pattern in self.patterns:
            hit, negate = self._match_pattern(pattern, rel, is_dir=is_dir)
            if not hit:
                continue
            ignored = not negate
        return ignored

    @classmethod
    def _match_pattern(cls, pattern: str, rel: str, *, is_dir: bool) -> tuple[bool, bool]:
        raw = str(pattern or "").strip()
        if not raw or raw.startswith("#"):
            return False, False
        negate = raw.startswith("!")
        if negate:
            raw = raw[1:].strip()
        dir_only = raw.endswith("/")
        if dir_only:
            raw = raw.rstrip("/")
            if not is_dir:
                return False, negate
        if not raw:
            return False, negate

        anchored = raw.startswith("/")
        if anchored:
            raw = raw.lstrip("/")

        if "/" not in raw:
            # 任意层级文件名/目录名
            parts = rel.split("/")
            hit = any(fnmatch.fnmatchcase(p, raw) or fnmatch.fnmatch(p, raw) for p in parts)
            return hit, negate

        hit = fnmatch.fnmatchcase(rel, raw) or fnmatch.fnmatch(rel, raw)
        if not hit and is_dir:
            hit = rel == raw or rel.startswith(raw + "/")
        if not hit:
            hit = fnmatch.fnmatchcase(rel, raw + "/*") or fnmatch.fnmatch(rel, raw + "/*")
        if anchored and not hit:
            hit = rel == raw or rel.startswith(raw + "/")
        return hit, negate
