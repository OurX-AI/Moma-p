"""macOS：sandbox-exec + Seatbelt profile。"""
from __future__ import annotations
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from .config import SandboxConfig
from .noop import NoopSandbox
from .runtime import SandboxRuntime, SandboxStatus, SandboxWrapResult


class MacSeatbeltSandbox(SandboxRuntime):
    """用 sandbox-exec 限制写盘：默认拒绝写，放行工作区与临时目录。"""

    def __init__(self, config: SandboxConfig, *, bash_path: str) -> None:
        self._config = config
        self._bash_path = bash_path

    @classmethod
    def from_config(cls, raw: dict) -> SandboxRuntime:
        cfg = SandboxConfig.from_dict(raw)
        if shutil.which("sandbox-exec") is None:
            return NoopSandbox(
                SandboxStatus.UNAVAILABLE,
                "sandbox-exec not found",
            )
        bash = shutil.which("bash") or "/bin/bash"
        if not Path(bash).is_file():
            return NoopSandbox(
                SandboxStatus.UNAVAILABLE,
                "bash not found for sandboxed execution",
            )
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
        profile = self._build_profile(root=root, work=work)
        profile_path = Path(tempfile.gettempdir()) / f"seatbelt_{os.getpid()}.sb"
        profile_path.write_text(profile, encoding="utf-8")
        parts = [
            "sandbox-exec",
            "-f",
            str(profile_path),
            self._bash_path,
            "-lc",
            command,
        ]
        wrapped = " ".join(shlex.quote(p) for p in parts)
        return SandboxWrapResult(
            command=wrapped,
            status=SandboxStatus.READY,
            detail=f"seatbelt profile={profile_path}",
        )

    def _build_profile(self, *, root: str, work: str) -> str:
        write_paths = [root, work, "/tmp", "/private/tmp", "/var/folders"]
        write_paths.extend(self._config.allow_write)
        # 去重并转义
        unique: list[str] = []
        seen: set[str] = set()
        for raw in write_paths:
            try:
                path = str(Path(raw).expanduser().resolve())
            except Exception:
                path = str(raw)
            if path in seen:
                continue
            seen.add(path)
            unique.append(path)

        deny_reads: list[str] = []
        deny_seen: set[str] = set()
        for raw in self._config.deny_read:
            try:
                path = str(Path(raw).expanduser().resolve())
            except Exception:
                path = str(raw).strip()
            if not path or path in deny_seen or path in (root, work, "/"):
                continue
            deny_seen.add(path)
            deny_reads.append(path)

        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal)",
            "(allow sysctl-read)",
            "(allow mach*)",
            "(allow file-read*)",
            "(allow file-write-data (literal \"/dev/null\"))",
            "(allow file-ioctl (literal \"/dev/null\"))",
        ]
        for path in deny_reads:
            escaped = path.replace("\\", "\\\\").replace("\"", "\\\"")
            lines.append(f'(deny file-read* (subpath "{escaped}"))')
            lines.append(f'(deny file-read* (literal "{escaped}"))')
        if not self._config.deny_network:
            lines.append("(allow network*)")
        for path in unique:
            escaped = path.replace("\\", "\\\\").replace("\"", "\\\"")
            lines.append(f'(allow file-write* (subpath "{escaped}"))')
        return "\n".join(lines) + "\n"
