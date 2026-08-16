import difflib
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional
from app.config.settings import settings


_RG_PATH: Optional[str] = None
_WINDOWS_RESERVED_BASENAME_RE = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


class ToolPathResolver:
    """将工具 path 解析为绝对路径：相对路径相对 workspace，而非进程 CWD。"""

    @staticmethod
    def resolve(path: str, workspace_path: str | None = None) -> Path:
        '''将工具 path 解析为绝对路径：相对路径相对 workspace，而非进程 CWD。'''
        raw = (path or "").strip()
        if not raw:
            raise ValueError("path must be a non-empty string")
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p.resolve()
        ws = (workspace_path or "").strip()
        if ws:
            return (Path(ws).expanduser().resolve() / p).resolve()
        return p.resolve()


def check_path_boundary(target: Path, workspace_path: str | None) -> Optional[str]:
    """校验 target 是否位于 workspace（或 runtime_data_dir）内，防止越界读写。

    返回 None 表示通过；返回字符串表示越界错误消息，由调用方转成 ToolErrorResult。
    settings.file_tool_workspace_boundary_enabled 关闭时不做限制。
    """
    # 开关关闭 -> 不做工作区限制
    if not settings.file_tool_workspace_boundary_enabled:
        return None

    ws = (workspace_path or "").strip()
    if not ws:
        return None  # 无 workspace 概念，跳过

    # 收集允许的根目录：workspace + runtime_data_dir
    allowed: List[Path] = []
    for raw in (ws, settings.runtime_data_dir):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            p = Path(raw).expanduser().resolve()
            if p not in allowed:
                allowed.append(p)
        except (OSError, ValueError):
            pass

    for root in allowed:
        try:
            target.relative_to(root)
            return None
        except ValueError:
            continue

    return (
        f"Path {target} is outside the allowed directories "
        f"({', '.join(str(r) for r in allowed)}). "
        "Use paths inside the workspace or MOMA runtime data directory."
    )


class WindowsReservedNameGuard:
    """拦截 Windows 保留设备名，避免在工作区生成 nul 等污染文件。"""

    @classmethod
    def is_reserved_basename(cls, path: str | Path) -> bool:
        '''判断是否为 Windows 保留设备名'''
        if sys.platform != "win32":
            return False
        
        # 判断是否为 Windows 保留设备名
        name = Path(path).name.strip().rstrip(".")
        if not name:
            return False
        return bool(_WINDOWS_RESERVED_BASENAME_RE.match(name))

    @classmethod
    def reject_message(cls, path: str) -> str:
        return (
            f"Rejected path {path!r}: Windows reserved device name "
            f"(CON/PRN/AUX/NUL/COM1-9/LPT1-9). Choose a normal filename."
        )


def rg_executable() -> Optional[str]:
    global _RG_PATH
    if _RG_PATH is None:
        _RG_PATH = shutil.which("rg") or ""
    return _RG_PATH or None


def resolve_search_dir(path: Optional[str], workspace_path: str) -> Path:
    '''将工具 path 解析为绝对路径：相对路径相对 workspace，而非进程 CWD。'''
    if path:
        return ToolPathResolver.resolve(path, workspace_path)
    ws = (workspace_path or "").strip()
    if ws:
        return Path(ws).expanduser().resolve()
    return Path.cwd().resolve()


def suggest_similar_paths(missing: Path, *, limit: int = 5) -> List[str]:
    name = missing.name
    parent = missing.parent
    if not parent.exists() or not parent.is_dir():
        return []

    names: List[str] = []
    try:
        for entry in parent.iterdir():
            if entry.is_file():
                names.append(entry.name)
    except OSError:
        return []

    if not names:
        return []

    picked = difflib.get_close_matches(name, names, n=limit, cutoff=0.5)
    if not picked:
        lower = name.lower()
        partial = [n for n in names if lower in n.lower() or n.lower() in lower]
        picked = partial[:limit]
    return [str(parent / n) for n in picked]


def format_not_found_message(requested: str, missing: Path) -> str:
    suggestions = suggest_similar_paths(missing)
    if not suggestions:
        return f"File not found: {requested}"
    lines = [f"File not found: {requested}", "Did you mean:"]
    lines.extend(f"  - {p}" for p in suggestions)
    return "\n".join(lines)
