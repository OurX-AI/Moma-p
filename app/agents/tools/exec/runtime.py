"""执行运行时：解析本机 bash / powershell，并按壳种创建子进程。

跨平台约定：
- Linux / macOS：主工具为 bash（找不到 bash 时回退 /bin/sh）
- Windows：bash 使用 Git Bash；powershell 默认可用
- powershell 是否暴露由 settings.use_powershell_tool（USE_POWERSHELL_TOOL）控制
- 工作目录由工具参数 working_dir 指定，缺省为 Agent workspace（与本模块无关）
"""
from __future__ import annotations
import asyncio
import os
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import Optional
from app.config.settings import settings


class ExecKind(str, Enum):
    BASH = "bash"
    POWERSHELL = "powershell"


class ExecRuntime:
    """解析本机可执行文件并创建子进程。"""

    @classmethod
    def resolve_bash(cls) -> Optional[str]:
        """自动探测可用 bash。

        - Windows：常见 Git 安装路径 / PATH 中的 bash
        - Linux / macOS：常见路径 + PATH（含 Homebrew）
        """
        if sys.platform == "win32":
            candidates = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
            ]
            for path in candidates:
                if path.is_file():
                    return str(path)
            return shutil.which("bash")

        candidates = [
            "/bin/bash",
            "/usr/bin/bash",
            "/opt/homebrew/bin/bash",
            "/usr/local/bin/bash",
        ]
        for path in candidates:
            if Path(path).is_file():
                return path
        return shutil.which("bash")

    @classmethod
    def resolve_posix_sh(cls) -> Optional[str]:
        """POSIX 上 bash 不可用时的兜底：/bin/sh 或 PATH 中的 sh。"""
        if sys.platform == "win32":
            return None
        for path in ("/bin/sh", "/usr/bin/sh"):
            if Path(path).is_file():
                return path
        return shutil.which("sh")

    @classmethod
    def resolve_powershell(cls) -> Optional[str]:
        """优先 pwsh（Core 7+），再 Windows PowerShell 5.x。"""
        for name in ("pwsh", "powershell"):
            found = shutil.which(name)
            if not found:
                continue
            if sys.platform.startswith("linux"):
                # 避开可能挂死的 snap 启动器，优先 apt/rpm 直装路径
                try:
                    resolved = str(Path(found).resolve())
                except OSError:
                    resolved = found
                if found.startswith("/snap/") or resolved.startswith("/snap/"):
                    for direct in ("/opt/microsoft/powershell/7/pwsh", "/usr/bin/pwsh"):
                        if Path(direct).is_file():
                            try:
                                if not str(Path(direct).resolve()).startswith("/snap/"):
                                    return direct
                            except OSError:
                                return direct
                    continue
            return found
        if sys.platform == "win32":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            desktop = (
                Path(system_root)
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            if desktop.is_file():
                return str(desktop)
        return None

    @classmethod
    def powershell_edition(cls) -> Optional[str]:
        """core = pwsh(7+)；desktop = Windows PowerShell 5.x。"""
        path = cls.resolve_powershell()
        if not path:
            return None
        base = Path(path).name.lower().replace(".exe", "")
        return "core" if base == "pwsh" else "desktop"

    @classmethod
    def is_bash_available(cls) -> bool:
        if cls.resolve_bash():
            return True
        # Linux / macOS：无 bash 时仍可用 /bin/sh 跑命令，工具仍暴露
        return cls.resolve_posix_sh() is not None

    @classmethod
    def is_powershell_tool_enabled(cls) -> bool:
        """是否向 Agent 暴露 powershell（settings.use_powershell_tool）。

        - 显式 true/false：按配置
        - 未设置：Windows 默认开，Linux/macOS 默认关（且本机须有 pwsh/powershell）
        """
        if not cls.resolve_powershell():
            return False
        flagged = settings.use_powershell_tool
        if flagged is False:
            return False
        if flagged is True:
            return True
        return sys.platform == "win32"

    @classmethod
    def primary_kind(cls) -> ExecKind:
        """上下文展示用的主壳：优先 bash，Windows 无 bash 时才是 powershell。"""
        if cls.is_bash_available():
            return ExecKind.BASH
        if cls.resolve_powershell():
            return ExecKind.POWERSHELL
        return ExecKind.BASH

    @classmethod
    def detect_runtime_shell_name(cls) -> str:
        """供 system prompt 的壳名，与真实主执行路径一致。"""
        if cls.primary_kind() == ExecKind.POWERSHELL:
            return "powershell"
        if sys.platform == "win32":
            return "bash" if cls.resolve_bash() else "powershell"

        bash = cls.resolve_bash()
        if bash:
            return "bash"
        shell_path = os.environ.get("SHELL") or ""
        shell_name = Path(shell_path).name.strip().lower()
        if "zsh" in shell_name:
            return "zsh"
        if "bash" in shell_name:
            return "bash"
        if cls.resolve_posix_sh():
            return "sh"
        return shell_name or "sh"

    @classmethod
    def unavailable_reason(cls, kind: ExecKind) -> Optional[str]:
        if kind == ExecKind.BASH:
            if not cls.is_bash_available():
                if sys.platform == "win32":
                    return (
                        "Error: Bash is unavailable. Install Git for Windows. "
                        "Use the powershell tool instead."
                    )
                return "Error: Neither bash nor /bin/sh is available on this host."
            return None
        if not cls.resolve_powershell():
            return (
                "Error: PowerShell is unavailable on this host. "
                "Install pwsh, or use the bash tool."
            )
        if not cls.is_powershell_tool_enabled():
            return (
                "Error: PowerShell tool is disabled. "
                "Set USE_POWERSHELL_TOOL=true in env, or use the bash tool."
            )
        return None

    @classmethod
    async def create_process(
        cls,
        command: str,
        cwd: str,
        *,
        kind: ExecKind,
    ) -> asyncio.subprocess.Process:
        popen_kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        reason = cls.unavailable_reason(kind)
        if reason:
            raise RuntimeError(reason)

        if kind == ExecKind.POWERSHELL:
            exe = cls.resolve_powershell()
            assert exe is not None
            return await asyncio.create_subprocess_exec(
                exe,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                cwd=cwd,
                **popen_kwargs,
            )

        bash = cls.resolve_bash()
        if bash:
            return await asyncio.create_subprocess_exec(
                bash,
                "-c",
                command,
                cwd=cwd,
                **popen_kwargs,
            )
        sh = cls.resolve_posix_sh()
        if sh:
            return await asyncio.create_subprocess_exec(
                sh,
                "-c",
                command,
                cwd=cwd,
                **popen_kwargs,
            )
        raise RuntimeError("Error: No bash/sh interpreter available.")
