"""命令共用执行器：权限裁决、peel cd、编码守卫、沙箱、超时、后台会话。"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from ...permissions import PermissionEngine
from ...sandbox import SandboxRuntime
from ...schemes import AgentContext, RuntimeContext
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult, ToolTimeoutResult
from .process_manager import PROCESS_MANAGER
from .command_policy import CommandPolicy
from .common import guard_command, peel_cd_prefix, resolve_working_dir
from .output import OutputFormatter
from .runtime import ExecKind, ExecRuntime


class CommandRunner:
    """bash / powershell 共用的命令执行逻辑。"""

    def __init__(
        self,
        *,
        kind: ExecKind,
        timeout: int = 120,
        restrict_to_workspace: bool = False,
        tool_label: str = "bash",
    ) -> None:
        # 危险命令由 permissions 裁决；此处仅保留 workspace 限制与执行态守卫
        self.kind = kind
        self.timeout = timeout
        self.restrict_to_workspace = restrict_to_workspace
        self.tool_label = tool_label

    async def run(
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
        _ = run_ctx, description, kwargs
        unavailable = ExecRuntime.unavailable_reason(self.kind)
        if unavailable:
            return ToolErrorResult(unavailable)

        cwd = resolve_working_dir(working_dir, agent_ctx.workspace_path)
        try:
            cwd_path = Path(cwd).expanduser().resolve()
        except Exception:
            return ToolErrorResult(f"Invalid working_dir: {cwd!r}")

        workspace_root = (agent_ctx.workspace_path or "").strip() or None
        # 模型常写 cd "abs" && cmd：在 workspace 内时剥成 working_dir
        command, peeled_cwd = peel_cd_prefix(command, str(cwd_path), workspace_root)
        if peeled_cwd != str(cwd_path):
            try:
                cwd_path = Path(peeled_cwd).expanduser().resolve()
            except Exception:
                return ToolErrorResult(f"Invalid working_dir after cd peel: {peeled_cwd!r}")

        permission_engine = PermissionEngine.from_agent_config(agent_ctx.agent_config)
        decision = permission_engine.decide(self.tool_label, command)
        if not decision.allowed:
            return ToolErrorResult(
                f"Error: Command blocked by permission policy ({decision.reason})"
            )

        guard_err = guard_command(
            command,
            str(cwd_path),
            restrict_to_workspace=self.restrict_to_workspace,
            workspace_root=workspace_root,
            background=background,
            tool_label=self.tool_label,
        )
        if guard_err:
            return ToolErrorResult(guard_err)

        server_block = CommandPolicy.foreground_server_block_reason(
            command,
            background=background,
            tool_label=self.tool_label,
        )
        if server_block:
            return ToolErrorResult(server_block)

        # --------------------------------------------------------------------------
        # bash 管道假绿修正：含管道的命令自动前置 set -o pipefail
        #
        # 问题：bash 默认管道退出码只取最后一个命令（cmd_b）。若 cmd_a 静默失败
        # 但 cmd_b 成功，整个管道报告成功（"假绿"），agent 看到 success 会误以为
        # 整条命令成功。例：`grep pattern missing_file | head -5` 中 grep 因文件
        # 不存在失败（exit 2），但 head 读到空输入正常退出（exit 0），管道报告 0。
        #
        # 解法：set -o pipefail 让管道退出码变为"最后一个失败命令"的退出码（全
        # 成功才是 0），真实失败得以暴露。这是 correctness 修正，不是偏好，故
        # 无配置开关；想关闭用 `set +o pipefail`，想忽略上游失败用 `|| true`。
        #
        # 触发条件（全部满足才前置）：
        #   1. 仅 bash 工具（powershell 管道语义不同，不适用）
        #   2. 命令含 |（单条命令不存在假绿问题）
        #   3. 用户未已手写 set -o pipefail（避免重复前置）
        #
        # 不破坏：PIPESTATUS 数组照常记录每条命令退出码，可单独读取。
        # --------------------------------------------------------------------------
        if (
            self.tool_label == "bash"
            and "|" in command
            and "set -o pipefail" not in command
        ):
            command = "set -o pipefail; " + command

        sandbox = SandboxRuntime.from_agent_config(agent_ctx.agent_config)
        wrap = sandbox.wrap(
            command,
            cwd=str(cwd_path),
            workspace_root=workspace_root,
        )
        command = wrap.command

        if background:
            try:
                session = await PROCESS_MANAGER.start(
                    command=command,
                    cwd=str(cwd_path),
                    agent_session_id=agent_ctx.session_id or "",
                    exec_kind=self.kind,
                )
                payload = {
                    "session_id": session.session_id,
                    "pid": session.process.pid,
                    "status": session.status,
                    "command": session.command,
                    "shell": self.kind.value,
                }
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logging.exception("Failed to start background command: %s", e)
                return ToolErrorResult(f"Error starting background command: {e}")

        if timeout is not None and timeout < 0:
            return ToolErrorResult(
                f"Invalid timeout value: {timeout}. Timeout must be a positive number."
            )
        timeout_sec = CommandPolicy.resolve_timeout_sec(
            command,
            timeout,
            default_sec=self.timeout,
        )

        try:
            process = await ExecRuntime.create_process(
                command,
                str(cwd_path),
                kind=self.kind,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return ToolTimeoutResult(
                    OutputFormatter.format_timeout(
                        timeout_sec,
                        hint_background=True,
                        tool_label=self.tool_label,
                    )
                )
            except asyncio.CancelledError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                raise

            returncode = process.returncode or 0
            stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            body, was_truncated = OutputFormatter.format_result(
                stdout=stdout_text,
                stderr=stderr_text,
                exit_code=returncode,
            )
            return ToolSuccessResult(
                body,
                metadata={
                    "truncated": was_truncated,
                    "exit_code": returncode,
                    "shell": self.kind.value,
                },
            )
        except Exception as e:
            msg = str(e) or repr(e)
            logging.error("Error executing command: %s (type=%s)", msg, type(e).__name__)
            return ToolErrorResult(f"Error executing command: {msg}")
