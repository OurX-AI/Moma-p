"""PowerShell 工具：Windows 默认可用；非 Windows 需显式开启。"""
from __future__ import annotations
import sys
from typing import Any
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult
from ...schemes import AgentContext, RuntimeContext
from .command_policy import CommandPolicy
from .output import OutputFormatter
from .runner import CommandRunner
from .runtime import ExecKind, ExecRuntime
from ..result_truncate_policy import ToolResultTruncateSpec


_SHARED_PARAMS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The PowerShell command to execute",
        },
        "working_dir": {
            "type": "string",
            "description": "Optional working directory; defaults to the agent workspace",
        },
        "timeout": {
            "type": "number",
            "description": (
                "Optional timeout in milliseconds (foreground only). "
                "When omitted: 120000ms default, or 600000ms for test/build commands."
            ),
        },
        "background": {
            "type": "boolean",
            "default": False,
            "description": "Run in background and return a session_id for shell_process",
        },
        "description": {
            "type": "string",
            "description": "Optional short description of what this command does.",
        },
    },
    "required": ["command"],
}


@register_tool(name="powershell", toolset="exec")
class PowerShellTool(BaseTool):
    """在 PowerShell 中执行命令；优先 pwsh，否则 Windows PowerShell 5.x。"""

    def __init__(
        self,
        timeout: int = 120,
        restrict_to_workspace: bool = False,
        agent_ctx: AgentContext | None = None,
    ):
        super().__init__(agent_ctx=agent_ctx)
        self._runner = CommandRunner(
            kind=ExecKind.POWERSHELL,
            timeout=timeout,
            restrict_to_workspace=restrict_to_workspace,
            tool_label="powershell",
        )

    @property
    def name(self) -> str:
        return "powershell"

    def is_available(self) -> bool:
        return ExecRuntime.is_powershell_tool_enabled()

    def is_readonly(self, params=None) -> bool:
        params = params or {}
        command = str(params.get("command") or "")
        if not command:
            return False
        return CommandPolicy.is_readonly_command(command)

    def is_parallel(self, params=None) -> bool:
        params = params or {}
        command = str(params.get("command") or "")
        if not command:
            return False
        background = bool(params.get("background") or False)
        return CommandPolicy.is_parallel_safe_command(command, background=background)

    def result_truncate_spec(self) -> ToolResultTruncateSpec:
        return ToolResultTruncateSpec(
            max_bytes=OutputFormatter.MAX_CHARS,
            direction="tail",
        )

    def validate_params(self, params: dict) -> list[str]:
        errors = super().validate_params(params)
        command = params.get("command")
        if isinstance(command, str) and not command.strip():
            errors.append(
                "[INVALID_VALUE] command must be a non-empty string. "
                "Provide the PowerShell command to run and retry."
            )
        timeout = params.get("timeout")
        if timeout is not None and isinstance(timeout, (int, float)) and timeout < 0:
            errors.append(
                "[OUT_OF_RANGE] timeout must be >= 0 (milliseconds). "
                "Omit timeout for defaults, or pass a positive value and retry."
            )
        return errors

    def description(self, params=None) -> str:
        edition = ExecRuntime.powershell_edition() or "desktop"
        base = """Execute a PowerShell command and return structured output.

When to use:
- Windows-native scripting and admin flows (registry, services, WinRM, PS modules).

When NOT to use:
- Typical build/test/git flows where `bash` is simpler and more stable.
- File read/search/edit/write: use `read_file`/`grep_search`/`glob_search`/`read_dir`/`edit_file`/`write_file` (not `Get-Content`/`Select-String`/`Set-Content` for routine coding).

Execution rules:
- `command` is required and must be non-empty.
- Use `working_dir` over inline directory switching when possible.
- Long-running services must use `background=true` and be managed with `shell_process`.
- Do not create Windows reserved device files (`nul`/`con`/...); discard output with `/dev/null` in bash, or omit file redirects.

Failure recovery:
- Parse/operator errors: simplify command, avoid mixed shell syntax, and execute in smaller steps.
- Timeout: raise `timeout` or run in background mode.
- Test assertion/import failures are task failures, not tool failures.

Output contract:
- Always returns `exit_code`, `stdout`, `stderr`.
- Long output keeps the tail for diagnostics.
"""
        if edition == "core":
            base += (
                " Runtime is PowerShell 7+ (pwsh): `&&` / `||` chaining is supported."
            )
        else:
            base += (
                " Runtime is Windows PowerShell 5.x: do NOT use `&&` / `||`; chain with `;`. "
                "Use `Select-Object -First/-Last` instead of head/tail."
            )
        if sys.platform != "win32":
            base += " This host is non-Windows; only use when PowerShell semantics are required."
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        return _SHARED_PARAMS

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        command: str,
        working_dir: str | None = None,
        timeout: float | None = None,
        background: bool = False,
        description: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        return await self._runner.run(
            agent_ctx,
            run_ctx,
            command=command,
            working_dir=working_dir,
            timeout=timeout,
            background=background,
            description=description,
            **kwargs,
        )
