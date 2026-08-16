"""沙箱抽象与平台工厂。"""
from __future__ import annotations
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SandboxStatus(str, Enum):
    READY = "ready"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SandboxWrapResult:
    """包装后的命令；unsupported/disabled 时 command 保持原样。"""

    command: str
    status: SandboxStatus
    detail: str = ""


class SandboxRuntime(ABC):
    """Bash/PowerShell 子进程沙箱。"""

    @abstractmethod
    def status(self) -> SandboxStatus:
        raise NotImplementedError

    @abstractmethod
    def wrap(
        self,
        command: str,
        *,
        cwd: str,
        workspace_root: str | None = None,
    ) -> SandboxWrapResult:
        raise NotImplementedError

    @classmethod
    def from_agent_config(cls, agent_config: dict | None) -> SandboxRuntime:
        raw = agent_config.get("sandbox") if isinstance(agent_config, dict) else None
        enabled = True
        if isinstance(raw, dict) and "enabled" in raw:
            enabled = bool(raw.get("enabled"))
        if not enabled:
            from .noop import NoopSandbox

            return NoopSandbox(SandboxStatus.DISABLED, "sandbox.enabled=false")

        if sys.platform == "win32":
            from .noop import NoopSandbox

            return NoopSandbox(
                SandboxStatus.UNSUPPORTED,
                "native Windows sandbox not supported (use WSL2 for bubblewrap)",
            )

        if sys.platform == "darwin":
            from .mac_seatbelt import MacSeatbeltSandbox

            return MacSeatbeltSandbox.from_config(raw if isinstance(raw, dict) else {})

        from .linux_bubblewrap import LinuxBubblewrapSandbox

        return LinuxBubblewrapSandbox.from_config(raw if isinstance(raw, dict) else {})
