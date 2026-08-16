"""权限规则：Tool / Tool(specifier)，Bash 支持 * 通配。"""
from __future__ import annotations
import fnmatch
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionRule:
    """单条权限规则。"""

    tool: str
    specifier: str | None = None
    raw: str = ""

    @classmethod
    def parse(cls, text: str) -> PermissionRule | None:
        raw = (text or "").strip()
        if not raw:
            return None
        m = re.fullmatch(r"([A-Za-z_][\w]*)(?:\((.*)\))?", raw)
        if not m:
            return None
        tool = m.group(1)
        spec = m.group(2)
        if spec is not None:
            spec = spec.strip()
            if spec == "*" or spec == "":
                spec = None
        return cls(tool=tool, specifier=spec, raw=raw)

    def matches_tool(self, tool_name: str) -> bool:
        return self.tool.lower() == (tool_name or "").strip().lower()

    def matches(self, tool_name: str, primary_input: str) -> bool:
        if not self.matches_tool(tool_name):
            return False
        if self.specifier is None:
            return True
        return _glob_match(self.specifier, primary_input or "")


def _glob_match(pattern: str, text: str) -> bool:
    """`*` 通配可出现在任意位置；`foo:*` 等价 `foo *`；路径另试归一化候选。"""
    pat = pattern
    if pat.endswith(":*") and not pat.endswith(r"\:*"):
        pat = pat[:-2] + " *"
    candidates = _path_candidates(text)
    for candidate in candidates:
        if fnmatch.fnmatchcase(candidate, pat):
            return True
        # `npm test *` 也匹配无额外参数的 `npm test`
        if pat.endswith(" *"):
            prefix = pat[:-2]
            if candidate == prefix or candidate.startswith(prefix + " "):
                return True
        # `**/.env` 也能挡住 `.env` / `dir/.env`
        if "**/" in pat:
            suffix = pat.split("**/", 1)[-1]
            if suffix and (
                fnmatch.fnmatchcase(candidate, suffix)
                or fnmatch.fnmatchcase(candidate, f"*/{suffix}")
                or candidate.endswith(f"/{suffix}")
            ):
                return True
    return False


def _path_candidates(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return [""]
    norm = raw.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    out: list[str] = []
    for item in (raw, norm, f"./{norm}" if norm else ""):
        if item and item not in out:
            out.append(item)
    return out


_COMPOUND_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|&|\||&|\n)\s*")


class CommandSplitter:
    """复合命令拆分：每段独立匹配规则。"""

    @staticmethod
    def split(command: str) -> list[str]:
        cmd = (command or "").strip()
        if not cmd:
            return []
        parts = [p.strip() for p in _COMPOUND_SPLIT_RE.split(cmd) if p and p.strip()]
        return parts or [cmd]
