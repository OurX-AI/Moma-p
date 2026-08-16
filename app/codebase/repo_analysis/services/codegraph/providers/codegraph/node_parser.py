"""解析 codegraph node 文本输出为结构化依赖与符号摘要。"""
from __future__ import annotations
import os
import re
from typing import Dict, List, Set, Tuple


class NodeOutputParser:
    """解析 `codegraph node ... --symbols-only` 的文本结果。"""

    _PATH_IN_PARENS = re.compile(r"\(([^()\n]+?):(\d+)\)")
    _USED_BY = re.compile(
        r"used by\s+\d+\s+files:\s*(.+?)(?:\n|$)",
        re.IGNORECASE,
    )
    _SYMBOL_LINE = re.compile(
        r"^-\s+`([^`]+)`\s+\(([^)]+)\)",
        re.MULTILINE,
    )
    _TRAIL_SECTION = re.compile(
        r"\*\*(Calls →|Called by ←)\*\*\s*(.+?)(?=\n\*\*|\n\n|\Z)",
        re.DOTALL,
    )

    @classmethod
    def normalize_rel_path(cls, path: str) -> str:
        return path.replace("\\", "/").lstrip("./")

    @classmethod
    def extract_paths_from_text(cls, text: str) -> List[str]:
        paths: List[str] = []
        seen: Set[str] = set()
        for match in cls._PATH_IN_PARENS.finditer(text or ""):
            raw = match.group(1).strip()
            if not raw or ":" in raw and len(raw) >= 2 and raw[1] == ":":
                # 跳过 Windows 盘符误伤：path 已在 group1
                pass
            normalized = cls.normalize_rel_path(raw)
            if not cls._looks_like_repo_path(normalized):
                continue
            if normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
        # used by 列表可能是逗号分隔的纯路径（无括号）
        used = cls._USED_BY.search(text or "")
        if used:
            chunk = used.group(1)
            chunk = re.sub(r",\s*\+\d+\s+more\s*$", "", chunk, flags=re.IGNORECASE)
            for part in chunk.split(","):
                candidate = cls.normalize_rel_path(part.strip())
                if not cls._looks_like_repo_path(candidate):
                    continue
                if candidate not in seen:
                    seen.add(candidate)
                    paths.append(candidate)
        return paths

    @classmethod
    def _looks_like_repo_path(cls, path: str) -> bool:
        if not path or path.startswith("<"):
            return False
        if "/" not in path and "\\" not in path:
            return False
        lower = path.lower()
        return any(
            lower.endswith(ext)
            for ext in (
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
                ".mjs",
                ".cjs",
                ".go",
                ".java",
                ".rs",
                ".c",
                ".h",
                ".cpp",
                ".cc",
                ".cxx",
                ".hpp",
                ".hh",
                ".hxx",
            )
        )

    @classmethod
    def parse_file_relations(cls, text: str, self_path: str) -> Tuple[List[str], List[str]]:
        """返回 (dependents, dependencies)。"""
        self_norm = cls.normalize_rel_path(self_path)
        dependents: List[str] = []
        dependencies: List[str] = []
        dep_seen: Set[str] = set()
        dcy_seen: Set[str] = set()

        for title, body in cls._TRAIL_SECTION.findall(text or ""):
            paths = cls.extract_paths_from_text(body)
            if "Called by" in title:
                target, seen = dependents, dep_seen
            else:
                target, seen = dependencies, dcy_seen
            for path in paths:
                if path == self_norm:
                    continue
                if path not in seen:
                    seen.add(path)
                    target.append(path)

        # 合并 used-by 头部中的 dependents（Trail 可能截断）
        used = cls._USED_BY.search(text or "")
        if used:
            chunk = used.group(1)
            chunk = re.sub(r",\s*\+\d+\s+more\s*$", "", chunk, flags=re.IGNORECASE)
            for part in chunk.split(","):
                candidate = cls.normalize_rel_path(part.strip())
                if not cls._looks_like_repo_path(candidate) or candidate == self_norm:
                    continue
                if candidate not in dep_seen:
                    dep_seen.add(candidate)
                    dependents.append(candidate)

        return dependents, dependencies

    @classmethod
    def parse_file_summary(cls, text: str, file_path: str) -> Dict[str, object]:
        """解析 Symbols 段为 classes / functions 结构。"""
        classes: Dict[str, Dict[str, object]] = {}
        functions: List[Dict[str, object]] = []
        current_class: str = ""
        for match in cls._SYMBOL_LINE.finditer(text or ""):
            name = match.group(1).strip()
            kind = match.group(2).strip().lower()
            if kind == "class":
                current_class = name
                classes[name] = {
                    "name": name,
                    "full_name": name,
                    "methods": [],
                }
            elif kind == "method":
                owner = current_class or ""
                if owner and owner in classes:
                    methods = classes[owner]["methods"]
                    assert isinstance(methods, list)
                    methods.append({"name": name})
                else:
                    functions.append({"name": name, "kind": "method"})
            elif kind in ("function", "func"):
                functions.append({"name": name})
                current_class = ""
            else:
                functions.append({"name": name, "kind": kind})

        return {
            "name": os.path.basename(file_path),
            "language": "python" if file_path.endswith(".py") else "",
            "classes": list(classes.values()),
            "functions": functions,
        }
