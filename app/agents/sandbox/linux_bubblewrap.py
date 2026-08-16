"""Linux / WSL2：bubblewrap 沙箱。"""
from __future__ import annotations
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from .config import SandboxConfig
from .noop import NoopSandbox
from .runtime import SandboxRuntime, SandboxStatus, SandboxWrapResult


class LinuxBubblewrapSandbox(SandboxRuntime):
    """用 bwrap 限制子进程写权限：系统只读，工作区与临时目录可写。"""

    def __init__(self, config: SandboxConfig, *, bash_path: str) -> None:
        self._config = config
        self._bash_path = bash_path

    @classmethod
    def from_config(cls, raw: dict) -> SandboxRuntime:
        cfg = SandboxConfig.from_dict(raw)
        if shutil.which("bwrap") is None:
            return NoopSandbox(
                SandboxStatus.UNAVAILABLE,
                "bubblewrap (bwrap) not installed",
            )
        bash = shutil.which("bash") or "/bin/bash"
        if not Path(bash).is_file() and not Path("/bin/bash").is_file():
            return NoopSandbox(
                SandboxStatus.UNAVAILABLE,
                "bash not found for sandboxed execution",
            )
        if not Path(bash).is_file():
            bash = "/bin/bash"
        return cls(cfg, bash_path=bash)

    def status(self) -> SandboxStatus:
        return SandboxStatus.READY

    def wrap(
        self,
        command: str,
        *,
        cwd: str,
        workspace_root: str | None = None,
    ) -> SandboxWrapResult:
        root = str(Path((workspace_root or cwd or "").strip() or cwd).resolve())
        work = str(Path(cwd).resolve())
        parts = [
            "bwrap",
            "--die-with-parent",
            "--ro-bind", "/", "/",
            "--bind", root, root,
        ]
        if work != root:
            parts.extend(["--bind", work, work])

        for temp_dir in self._temp_dirs():
            parts.extend(["--bind", temp_dir, temp_dir])

        for extra in self._config.allow_write:
            try:
                path = str(Path(extra).expanduser().resolve())
            except Exception:
                continue
            if path and path not in (root, work):
                parts.extend(["--bind", path, path])

        # denyRead：用 tmpfs 覆盖路径，隐藏原内容（文件/目录均可）
        for denied in self._config.deny_read:
            try:
                path = str(Path(denied).expanduser().resolve())
            except Exception:
                path = str(denied).strip()
            if not path or path in (root, work, "/"):
                continue
            parts.extend(["--tmpfs", path])

        parts.extend(["--dev", "/dev", "--proc", "/proc"])
        if self._config.deny_network:
            parts.append("--unshare-net")
        parts.extend(["--chdir", work, "--", self._bash_path, "-lc", command])
        wrapped = " ".join(shlex.quote(p) for p in parts)
        detail = "bubblewrap ready"
        if self._config.deny_read:
            detail = f"bubblewrap ready; denyRead={len(self._config.deny_read)}"
        return SandboxWrapResult(
            command=wrapped,
            status=SandboxStatus.READY,
            detail=detail,
        )

    @staticmethod
    def _temp_dirs() -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for raw in ("/tmp", "/var/tmp", tempfile.gettempdir(), os.environ.get("TMPDIR") or ""):
            text = (raw or "").strip()
            if not text:
                continue
            try:
                path = str(Path(text).expanduser().resolve())
            except Exception:
                continue
            if path in seen:
                continue
            if Path(path).is_dir():
                seen.add(path)
                found.append(path)
        return found
