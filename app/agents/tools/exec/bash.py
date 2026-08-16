"""Bash 工具：POSIX shell（Windows 上为 Git Bash）。"""
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
            "description": "The bash command to execute",
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


@register_tool(name="bash", toolset="exec")
class BashTool(BaseTool):
    """在 Bash（POSIX）中执行命令；Windows 使用 Git Bash。"""

    def __init__(
        self,
        timeout: int = 120,
        restrict_to_workspace: bool = False,
        agent_ctx: AgentContext | None = None,
    ):
        super().__init__(agent_ctx=agent_ctx)
        self._runner = CommandRunner(
            kind=ExecKind.BASH,
            timeout=timeout,
            restrict_to_workspace=restrict_to_workspace,
            tool_label="bash",
        )

    @property
    def name(self) -> str:
        return "bash"

    def is_available(self) -> bool:
        return ExecRuntime.is_bash_available()

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
                "Provide the shell command to run and retry."
            )
        timeout = params.get("timeout")
        if timeout is not None and isinstance(timeout, (int, float)) and timeout < 0:
            errors.append(
                "[OUT_OF_RANGE] timeout must be >= 0 (milliseconds). "
                "Omit timeout for defaults, or pass a positive value and retry."
            )
        return errors

    def description(self, params=None) -> str:
        base = """Execute a bash (POSIX) command and return structured output.

When to use:
- Tests, builds, linters, package managers, git, and POSIX shell workflows.

When NOT to use (use dedicated tools instead):
- Read file content: use `read_file` (not `cat`/`head`/`tail`/`sed -n`).
- Search content: use `grep_search` (not `grep`/`rg`).
- Find files by name: use `glob_search` (not `find`/`ls -R`).
- List one directory: use `read_dir` (not `ls` for listing alone).
- Edit/write files: use `edit_file`/`write_file`/`apply_patch` (not `sed`/`awk`/`echo >file`/heredoc).

Execution rules:
- `command` is required and must be non-empty.
- Keep `command` text ASCII-only: no Chinese/non-ASCII in echo/comments/strings inside the shell command. Put human language in the chat, not in bash argv.
- Prefer short commands and multi-step calls over long one-liners.
- Use `working_dir` instead of `cd ... && ...` when possible.
- Quote paths containing spaces with double quotes.
- `timeout` is milliseconds (foreground only).
- Long-running services must use `background=true` and be managed by `shell_process`.
- Do not use shell background operators (`&`, `nohup`, `disown`, `start /b`).
- Do not redirect to bare `nul` / create files named CON/PRN/AUX/NUL; discard output with `/dev/null`.

Failure recovery:
- Exit 127 with garbled non-ASCII in stderr: rewrite the command in ASCII and retry (do not put localized text into bash -c).
- Exit 127 / unexpected EOF / no such file from quoting: validate quotes, split long one-liners, retry with simpler steps.
- Tool timeout: increase `timeout` or switch to background execution.
- Assertion/import failures from tests are task-level failures, not bash-tool failures.
- Need to inspect source after a test failure: call `read_file`/`grep_search`, not `cat`/`sed`.

Output contract:
- Always returns `exit_code`, `stdout`, `stderr`.
- Long output keeps the tail where failure traces usually appear.
"""
        if sys.platform == "win32":
            base += (
                " On Windows this runs Git Bash: `&&`, `head`, `tail`, and POSIX paths work."
                " Prefer `/dev/null` over cmd-style `nul` redirects."
            )
        else:
            bash = ExecRuntime.resolve_bash()
            if bash:
                base += f" Commands run via `{bash} -c` (Linux/macOS)."
            else:
                sh = ExecRuntime.resolve_posix_sh() or "/bin/sh"
                base += f" bash not found; commands run via `{sh} -c`."
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
