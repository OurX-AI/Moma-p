"""无沙箱 / 不支持平台时的透传实现。"""
from __future__ import annotations
from .runtime import SandboxRuntime, SandboxStatus, SandboxWrapResult


class NoopSandbox(SandboxRuntime):
    """不包装命令，仅报告状态。"""

    def __init__(self, status: SandboxStatus, detail: str = "") -> None:
        self._status = status
        self._detail = detail

    def status(self) -> SandboxStatus:
        return self._status

    def wrap(
        self,
        command: str,
        *,
        cwd: str,
        workspace_root: str | None = None,
    ) -> SandboxWrapResult:
        _ = cwd, workspace_root
        return SandboxWrapResult(command=command, status=self._status, detail=self._detail)
