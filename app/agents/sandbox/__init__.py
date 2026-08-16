"""沙箱运行时：macOS / Linux / WSL2 可启用；原生 Windows 不支持。"""
from __future__ import annotations

from .runtime import SandboxRuntime, SandboxStatus, SandboxWrapResult
from .config import SandboxConfig

__all__ = [
    "SandboxConfig",
    "SandboxRuntime",
    "SandboxStatus",
    "SandboxWrapResult",
]
