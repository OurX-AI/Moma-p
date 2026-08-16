import os
import re
import tempfile
from pathlib import Path

FOREGROUND_BACKGROUND_PATTERNS = [
    r"&\s*$",
    r"\bnohup\b",
    r"\bdisown\b",
    r"\bstart\s+/b\b",
]

MAX_BACKGROUND_STREAM_CHARS = 1_000_000

# 设备节点与临时目录：不因字符串扫描误拦
_POSIX_DEVICE_PREFIXES = (
    "/dev/null",
    "/dev/zero",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/fd",
)
_POSIX_TEMP_PREFIXES = (
    "/tmp",
    "/var/tmp",
)

# cd "path" && cmd  /  cd /d "path" && cmd  /  cd path && cmd（无空格无引号）
_CD_PREFIX_RE = re.compile(
    r"""^\s*cd\s+(?:/d\s+)?(?:
        (?P<q>["'])(?P<quoted>.*?)(?P=q)
        |
        (?P<unquoted>[^\s;&|]+)
    )\s*(?:&&|;)\s*""",
    re.IGNORECASE | re.VERBOSE,
)


class ExecPathAllowlist:
    """命令字符串中出现的区外路径白名单（设备节点 / 临时目录）。"""

    @staticmethod
    def normalize_posix_like(raw: str) -> str:
        text = (raw or "").strip().replace("\\", "/")
        if len(text) > 1:
            text = text.rstrip("/")
        return text.lower()

    @classmethod
    def is_device_path(cls, raw: str) -> bool:
        norm = cls.normalize_posix_like(raw)
        for prefix in _POSIX_DEVICE_PREFIXES:
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        return False

    @classmethod
    def is_temp_path_text(cls, raw: str) -> bool:
        norm = cls.normalize_posix_like(raw)
        for prefix in _POSIX_TEMP_PREFIXES:
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        return False

    @staticmethod
    def temp_roots() -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        candidates: list[str] = [tempfile.gettempdir()]
        for key in ("TMPDIR", "TEMP", "TMP"):
            value = (os.environ.get(key) or "").strip()
            if value:
                candidates.append(value)
        for raw in candidates:
            try:
                resolved = Path(raw).expanduser().resolve()
            except Exception:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            roots.append(resolved)
        return roots

    @classmethod
    def is_benign_external_path(cls, raw: str, resolved: Path) -> bool:
        """区外路径是否应放行：/dev/*、/tmp、系统 TEMP。"""
        if cls.is_device_path(raw) or cls.is_temp_path_text(raw):
            return True
        for root in cls.temp_roots():
            if _path_within_root(resolved, root) or resolved == root:
                return True
        return False


def resolve_working_dir(working_dir: str | None, workspace_path: str | None) -> str:
    if working_dir:
        return working_dir
    ws = (workspace_path or "").strip()
    if ws:
        return ws
    return os.getcwd()


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return path.resolve() == root.resolve()


def _extract_windows_abs_paths(cmd: str) -> list[str]:
    """提取命令中的 Windows 绝对路径（保留完整路径，不被 \\ 或空格误截断）。"""
    found: list[str] = []
    for m in re.finditer(r'["\']([A-Za-z]:\\[^"\']+)["\']', cmd):
        found.append(m.group(1))
    # 去掉已引号包裹段后再扫未加引号路径，避免空格路径在引号内被截成前半段
    masked = re.sub(r'["\'][^"\']*["\']', '""', cmd)
    for m in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z]:\\[^\s\"'<>|&]+)", masked):
        raw = m.group(1)
        if raw not in found:
            found.append(raw)
    return found


def _extract_posix_abs_paths(cmd: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd):
        found.append(m.group(1))
    return found


def peel_cd_prefix(
    command: str,
    cwd: str,
    workspace_root: str | None = None,
) -> tuple[str, str]:
    """若命令以 cd <workspace内路径> &&|; 开头，则剥掉并把 cwd 切到该目录。"""
    cmd = command.strip()
    m = _CD_PREFIX_RE.match(cmd)
    if not m:
        return command, cwd
    target_raw = ((m.group("quoted") if m.group("quoted") is not None else m.group("unquoted")) or "").strip()
    if not target_raw:
        return command, cwd
    try:
        cwd_path = Path(cwd).expanduser().resolve()
        root = Path(workspace_root).expanduser().resolve() if workspace_root else cwd_path
        target_path = Path(target_raw).expanduser()
        # 相对路径相对当前 working_dir 解析，避免跟进程 cwd 绑死
        if target_path.is_absolute():
            target = target_path.resolve()
        else:
            target = (cwd_path / target_path).resolve()
    except Exception:
        return command, cwd
    if not target.is_dir():
        return command, cwd
    if not _path_within_root(target, root):
        return command, cwd
    rest = cmd[m.end() :].strip()
    if not rest:
        return command, cwd
    return rest, str(target)


# Git Bash / cmd 把 `> nul` 写成普通文件名时，会污染工作区并触发 Windows \\.\nul
_WINDOWS_NUL_REDIRECT_RE = re.compile(
    r"(?:^|[\s;|&])(?:\d*)>\s*['\"]?(?:\.[\\/]+)?nul['\"]?(?:\s|$)",
    re.IGNORECASE,
)
_WINDOWS_TOUCH_RESERVED_RE = re.compile(
    r"(?:^|[\s;|&])touch\s+['\"]?(?:\.[\\/]+)?(?:con|prn|aux|nul|com[1-9]|lpt[1-9])['\"]?(?:\s|$)",
    re.IGNORECASE,
)


def guard_command(
    command: str,
    cwd: str,
    *,
    restrict_to_workspace: bool,
    workspace_root: str | None = None,
    background: bool = False,
    tool_label: str = "bash",
) -> str | None:
    cmd = command.strip()
    lower = cmd.lower()

    if not background:
        for pattern in FOREGROUND_BACKGROUND_PATTERNS:
            if re.search(pattern, lower):
                return (
                    f"Error: Use {tool_label}(background=true) for background tasks; "
                    "do not use shell-level &, nohup, disown, or start /b"
                )

    # 去掉引号串再扫，避免误伤 echo "use > nul carefully" 这类说明文本
    cmd_unquoted = re.sub(r'["\'][^"\']*["\']', '""', cmd)
    if _WINDOWS_NUL_REDIRECT_RE.search(cmd_unquoted) or _WINDOWS_TOUCH_RESERVED_RE.search(
        cmd_unquoted
    ):
        return (
            "Error: Command blocked — Windows reserved device name (e.g. `nul`). "
            "Use `/dev/null` for discarding output; do not create files named "
            "CON/PRN/AUX/NUL/COM1-9/LPT1-9."
        )

    if restrict_to_workspace:
        if "..\\" in cmd or "../" in cmd:
            return "Error: Command blocked by safety guard (path traversal detected)"

        root_path = None
        if workspace_root:
            try:
                root_path = Path(workspace_root).expanduser().resolve()
            except Exception:
                return "Error: Command blocked by safety guard (invalid workspace root)"

        try:
            cwd_path = Path(cwd).expanduser().resolve()
        except Exception:
            return "Error: Command blocked by safety guard (invalid working directory)"

        if root_path is not None and not _path_within_root(cwd_path, root_path):
            return "Error: Command blocked by safety guard (working dir outside workspace)"

        check_root = root_path or cwd_path
        candidates = _extract_windows_abs_paths(cmd) + _extract_posix_abs_paths(cmd)
        for raw in candidates:
            try:
                p = Path(raw.strip()).expanduser().resolve()
            except Exception:
                continue
            if not p.is_absolute():
                continue
            if _path_within_root(p, check_root):
                continue
            # temp/device 放行；其余区外绝对路径仍拦
            if ExecPathAllowlist.is_benign_external_path(raw, p):
                continue
            return "Error: Command blocked by safety guard (path outside workspace)"

    return None
