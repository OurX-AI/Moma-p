"""Rich-based REPL，替代 Textual TUI，使用终端原生滚动。"""
from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from app.agents.internal_dispatch import set_internal_message_handler
from app.agents.output import OutboundMessage, OutboundMessageType, set_output_handler
from app.agents.sessions.manager import SESSION_MANAGER
from app.agents.sessions.message import Role
from app.config.settings import APP_VERSION
from app.cli.display_format import format_outbound_content
from app.cli.runner import AGENT_RUNNER, AgentRunner
from app.cli.tui.logo import MOMA_LOGO
from app.cli.tui.slash_commands import SLASH_COMMANDS

_HISTORY_DIR = Path.home() / ".moma" / "history"


def _ensure_history_dir() -> Path:
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return _HISTORY_DIR


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"</think\s*>", re.IGNORECASE)


def _strip_think(text: str, *, drop_open: bool = False) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", text or "")
    if drop_open:
        cleaned = _THINK_OPEN_RE.sub("", cleaned)
    cleaned = _THINK_CLOSE_RE.sub("", cleaned)
    return cleaned


class EnhancedTerminalOutputHandler:
    """Rich 格式化输出处理器：流式 delta 直写 stdout，非流式消息走 Rich Console。"""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._stream_open = False
        self._stream_buf = ""
        self._last_visible = ""

    async def __call__(self, msg: OutboundMessage) -> None:
        outbound_type = msg.outbound_type

        if outbound_type == OutboundMessageType.STREAM_START:
            if self._stream_open:
                return
            self._stream_open = True
            self._stream_buf = ""
            return

        if outbound_type == OutboundMessageType.STREAM_DELTA:
            if not msg.content:
                return
            if not self._stream_open:
                self._stream_open = True
                self._stream_buf = ""
            self._stream_buf += msg.content
            visible = _strip_think(self._stream_buf, drop_open=True)
            prev = self._last_visible
            if visible.startswith(prev):
                delta = visible[len(prev):]
            else:
                delta = visible
            self._last_visible = visible
            if delta:
                sys.stdout.write(delta)
                sys.stdout.flush()
            return

        if outbound_type == OutboundMessageType.STREAM_END:
            self._stream_open = False
            self._stream_buf = ""
            self._last_visible = ""
            return

        if outbound_type == OutboundMessageType.RUN_END:
            if self._stream_open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._stream_open = False
                self._stream_buf = ""
                self._last_visible = ""
            return

        # 非流式消息：Rich 格式化
        if msg.content:
            if self._stream_open:
                sys.stdout.write("\n")
                self._stream_open = False
                self._stream_buf = ""
                self._last_visible = ""
            text = _strip_think(msg.content).strip()
            if not text:
                return
            kind, formatted = format_outbound_content(text)
            if kind == "skip" or not formatted:
                return
            if kind == "tool":
                self._console.print(f"  \u25b6 {formatted}", style="dim")
            elif kind == "ask":
                self._console.print(f"  \u2753 {formatted}", style="bold yellow")
            else:
                try:
                    self._console.print(Markdown(formatted))
                except Exception:
                    self._console.print(formatted)


class ReplApp:
    """Rich-based REPL，替代 Textual TUI。"""

    def __init__(
        self,
        *,
        session_id: Optional[str],
        workspace: Path,
        runner: AgentRunner,
        user_id: str,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self._session_id = session_id
        self._workspace = workspace
        self._runner = runner
        self._user_id = user_id
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._console = Console()
        self._agent_busy = False
        self._pending_queues: dict[str, list[str]] = {}
        self._should_exit = False
        self._ctrl_c_pending = False

    def _setup_prompt(self) -> None:
        """配置 prompt_toolkit：输入 / 时弹出命令菜单，↑↓ 选择，历史持久化。"""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        commands = SLASH_COMMANDS.all()
        history_dir = _ensure_history_dir()
        history = FileHistory(history_dir / "input_history")

        class SlashCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/"):
                    return
                for cmd in commands:
                    trigger = f"/{cmd.name}"
                    if trigger.startswith(text):
                        yield Completion(
                            trigger,
                            start_position=-len(text),
                            display_meta=cmd.summary,
                        )

        bindings = KeyBindings()

        @bindings.add("c-c")
        def _(event):
            event.app.exit(exception=KeyboardInterrupt)

        self._prompt_session = PromptSession(
            message=HTML("<ansiyellow>></ansiyellow> "),
            completer=SlashCompleter(),
            complete_while_typing=True,
            key_bindings=bindings,
            history=history,
        )

    async def run(self) -> None:
        self._print_welcome()
        self._setup_prompt()
        set_output_handler(EnhancedTerminalOutputHandler(self._console))
        set_internal_message_handler(self._runner.handle_internal_message)
        self._install_signal_handler()

        while not self._should_exit:
            try:
                loop = asyncio.get_event_loop()
                line = await loop.run_in_executor(
                    None, lambda: self._prompt_session.prompt()
                )
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                break

            text = line.strip()
            if not text:
                continue

            if text.startswith("/"):
                if text == "/":
                    self._print_available_commands()
                    continue
                try:
                    should_continue = await self._handle_command(text)
                except KeyboardInterrupt:
                    sys.stdout.write("\n")
                    continue
                if not should_continue:
                    break
                continue

            try:
                await self._run_turn(text)
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                if self._agent_busy:
                    msg = await self._runner.stop_current()
                    self._console.print(msg, style="yellow")
                    self._console.print(
                        "Interrupted. Press Ctrl+C again to exit.", style="dim"
                    )

    def _print_welcome(self) -> None:
        self._console.print(MOMA_LOGO)
        self._console.print(
            f"MOMA v{APP_VERSION} | workspace: {self._workspace}", style="dim"
        )
        model = self._format_model()
        self._console.print(f"Model: {model}", style="dim")
        self._console.print(
            "Type / for commands, or ask a question\n", style="dim"
        )

    def _format_model(self) -> str:
        provider = (self._llm_provider or "").strip()
        model = (self._llm_model or "").strip()
        if provider and model:
            return f"{provider}/{model}"
        if model:
            return model
        return "default"

    def _pending_for(self, session_id: str) -> list[str]:
        return self._pending_queues.setdefault(session_id, [])

    def _print_status_line(self) -> None:
        """每轮结束后打印简洁状态行。"""
        state = self._runner.agent_state(self._session_id)
        session_short = self._session_id.split("_")[-1][:8] if self._session_id else "---"
        model = self._format_model()
        status_style = "green" if state == "IDLE" else "yellow"
        self._console.print(
            f"  [{status_style}]{state.lower()}[/{status_style}]  "
            f"session:{session_short}  model:{model}",
            style="dim",
        )

    def _print_available_commands(self) -> None:
        """显示所有可用的斜杠命令。"""
        self._console.print("Available commands:", style="bold")
        for cmd in SLASH_COMMANDS.all():
            self._console.print(f"  /{cmd.name:<12} {cmd.summary}", style="dim")

    # ===== 斜杠命令 =====

    async def _handle_command(self, line: str) -> bool:
        raw = line.strip()
        parts = raw.split(maxsplit=1)
        name = parts[0][1:].casefold() if parts and parts[0].startswith("/") else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name in ("exit", "quit", "q"):
            return False

        if name in ("help", "commands"):
            self._console.print(Markdown(SLASH_COMMANDS.help_markdown()))
            return True

        if name == "status":
            self._print_status()
            return True

        if name == "stop":
            msg = await self._runner.stop_current()
            self._console.print(msg)
            return True

        if name == "kill":
            msg = await self._runner.stop_current(hard=True)
            self._console.print(msg)
            return True

        if name == "new":
            self._session_id = None
            self._runner.bind_session(None)
            self._agent_busy = False
            self._console.print("New session: type a message to start.", style="green")
            return True

        if name == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            self._print_welcome()
            return True

        if name == "model":
            await self._handle_model_command(arg)
            return True

        if name in ("resume", "continue"):
            if arg:
                await self._resume_session(arg)
            else:
                await self._open_resume_picker()
            return True

        if name == "codebase":
            await self._handle_codebase_command(arg)
            return True

        if name == "queue":
            await self._handle_queue_command(arg)
            return True

        known = SLASH_COMMANDS.resolve(raw)
        if known is None:
            self._console.print(
                f"Unknown command: {line}\nType /help for available commands.",
                style="red",
            )
        else:
            self._console.print(
                f"Command /{known.name} is not available yet.", style="dim"
            )
        return True

    async def _handle_model_command(self, arg: str) -> None:
        if not arg:
            self._console.print(f"Current model: {self._format_model()}")
            self._console.print("Usage: /model provider/model_name")
            return
        if "/" in arg:
            provider, model = arg.split("/", 1)
            provider, model = provider.strip(), model.strip()
        else:
            provider, model = "", arg.strip()
        self._llm_provider = provider
        self._llm_model = model
        if self._session_id:
            await SESSION_MANAGER.update_session(
                self._session_id, llm_provider=provider, llm_model=model
            )
            self._runner.drop_agent(self._session_id)
        self._console.print(f"Model set to: {self._format_model()}", style="green")

    async def _handle_queue_command(self, arg: str) -> None:
        queue = self._pending_for(self._session_id or "")
        if arg == "clear":
            n = len(queue)
            queue.clear()
            if n:
                self._console.print(f"Cleared {n} queued message(s).", style="green")
            else:
                self._console.print("Queue is empty.", style="dim")
            return
        if not queue:
            self._console.print("Queue is empty.", style="dim")
            return
        self._console.print(f"Queue ({len(queue)} waiting):")
        for i, msg in enumerate(queue, 1):
            preview = msg if len(msg) <= 60 else msg[:57] + "..."
            self._console.print(f"  {i}. {preview}")

    # ===== Session 管理 =====

    async def _open_resume_picker(self) -> None:
        if self._agent_busy:
            self._console.print(
                "Stop the current task before switching sessions.", style="yellow"
            )
            return
        sessions = await self._list_cli_sessions()
        if not sessions:
            self._console.print("No CLI sessions yet.", style="dim")
            return
        self._console.print("Recent sessions:")
        for i, s in enumerate(sessions[:20], 1):
            sid = s.session_id
            stamp = ""
            last_updated = getattr(s, "last_updated", None)
            if last_updated is not None:
                stamp = last_updated.strftime("%m-%d %H:%M")
            title = s.display_title(40)
            marker = " *" if sid == self._session_id else ""
            short_id = sid[-8:]
            label = f"{stamp}  {title} [{short_id}]{marker}" if stamp else f"{title} [{short_id}]{marker}"
            self._console.print(f"  {i}. {label}")
        try:
            loop = asyncio.get_event_loop()
            choice = await loop.run_in_executor(
                None, lambda: input("Select session number or full ID: ")
            )
        except (EOFError, KeyboardInterrupt):
            return
        choice = choice.strip()
        if not choice:
            return
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(sessions):
                await self._resume_session(sessions[idx].session_id)
            else:
                self._console.print("Invalid selection.", style="red")
        else:
            await self._resume_session(choice)

    async def _resume_session(self, session_id: str) -> None:
        target = (session_id or "").strip()
        if not target:
            self._console.print("Usage: /resume [session_id]")
            return
        if target == self._session_id:
            self._console.print(f"Already on session: {target}")
            return
        session = await SESSION_MANAGER.get_session(target)
        if session is None:
            sessions = await self._list_cli_sessions()
            matches = [
                s for s in sessions
                if s.session_id == target or s.session_id.endswith(target)
            ]
            if len(matches) == 1:
                session = matches[0]
                target = session.session_id
            elif len(matches) > 1:
                self._console.print(
                    f"Ambiguous session id `{target}`; use full id from /resume.",
                    style="yellow",
                )
                return
            else:
                self._console.print(f"Session not found: {target}", style="red")
                return
        self._runner.bind_session(target)
        self._session_id = target
        self._llm_provider = getattr(session, "llm_provider", "") or self._llm_provider
        self._llm_model = getattr(session, "llm_model", "") or self._llm_model
        ws = getattr(session, "workspace_path", None) or ""
        if ws:
            self._workspace = Path(ws).expanduser().resolve()
        target_state = self._runner.agent_state(target)
        self._agent_busy = target_state != "IDLE"
        os.system("cls" if os.name == "nt" else "clear")
        self._print_welcome()
        await self._render_session_preview(target)
        self._console.print(f"Resumed session: {target}", style="green")
        self._print_status_line()

    async def _list_cli_sessions(self) -> list:
        sessions = await SESSION_MANAGER.get_all_sessions(
            channel_type="cli", user_id=self._user_id,
        )
        visible = [s for s in sessions if not getattr(s, "is_internal", False)]
        visible.sort(
            key=lambda s: getattr(s, "last_updated", None) or datetime.min,
            reverse=True,
        )
        return visible

    async def _render_session_preview(self, session_id: str) -> None:
        messages = await SESSION_MANAGER.get_messages(session_id)
        visible: list = []
        for m in messages:
            role = getattr(m, "role", None)
            if role is None:
                continue
            if role == Role.ASSISTANT and getattr(m, "tool_calls", None) and not (m.content or "").strip():
                continue
            if role == Role.TOOL and not (m.name and m.tool_call_id):
                continue
            if role in (Role.USER, Role.SYSTEM) and not (m.content or "").strip():
                continue
            visible.append(m)
        preview = visible[-200:]
        if not preview:
            return
        for msg in preview:
            if msg.role == Role.USER:
                self._console.print(f"You: {msg.content.strip()}", style="bold")
            elif msg.role == Role.ASSISTANT:
                content = (msg.content or "").strip()
                if content:
                    self._console.print("MOMA", style="bold #c15f3c")
                    try:
                        self._console.print(Markdown(content))
                    except Exception:
                        self._console.print(content)
            elif msg.role == Role.TOOL:
                payload = msg.to_user_message().get("content", "")
                kind, formatted = format_outbound_content(payload)
                if kind == "tool" and formatted:
                    success = getattr(msg, "success", None)
                    icon = "\u2713" if success is True else ("\u2717" if success is False else "\u25b6")
                    self._console.print(f"  {icon} {formatted}", style="dim")

    # ===== CodeBase 命令 =====

    async def _handle_codebase_command(self, arg: str) -> None:
        if not arg:
            self._console.print(
                "CodeBase 子命令:\n"
                "  /codebase status            查看分析进度（默认）\n"
                "  /codebase rescan            强制重新扫描\n"
                "  /codebase experience        查看 MR 经验提取状态\n"
                "  /codebase experience export 导出经验到 JSON 文件"
            )
            return
        if arg.startswith("experience"):
            sub_arg = arg[len("experience"):].strip()
            await self._handle_experience_command(sub_arg)
            return
        if arg == "rescan":
            from app.codebase.integration.orchestrator import AutoAnalyzeOrchestrator
            try:
                await AutoAnalyzeOrchestrator.trigger_reanalyze(
                    workspace_path=str(self._workspace), user_id=self._user_id,
                )
            except Exception as exc:
                self._console.print(f"触发失败: {exc}", style="red")
                return
            self._console.print("已触发 CodeBase 重新扫描。", style="green")
        from app.codebase.integration.status_formatter import format_codebase_status
        try:
            text = await format_codebase_status(str(self._workspace), self._user_id)
        except Exception as exc:
            self._console.print(f"读取 CodeBase 状态失败: {exc}", style="red")
            return
        self._console.print(text)

    async def _handle_experience_command(self, arg: str) -> None:
        if arg.strip() == "export":
            await self._export_experience()
            return
        from app.codebase.integration.status_formatter import format_experience_status
        try:
            text = await format_experience_status(str(self._workspace))
        except Exception as exc:
            self._console.print(f"读取经验提取状态失败: {exc}", style="red")
            return
        self._console.print(text)

    async def _export_experience(self) -> None:
        from sqlalchemy import select
        from app.codebase.repo_analysis.models.experience_status import MrExperienceItem
        from app.codebase.repo_mgmt.services.repo_resolver import RepoResolver
        from app.infrastructure.database import get_db_session

        async with get_db_session() as db:
            repo = await RepoResolver.get_by_path(db, str(self._workspace))
            if not repo:
                self._console.print("workspace 未注册", style="red")
                return
            items = (await db.scalars(
                select(MrExperienceItem)
                .where(MrExperienceItem.repo_id == repo.id)
                .where(MrExperienceItem.status == "ready")
                .order_by(MrExperienceItem.committed_at.desc())
            )).all()

        if not items:
            self._console.print("暂无已提取的经验", style="dim")
            return

        export_dir = Path(self._workspace) / ".moma"
        export_dir.mkdir(exist_ok=True)
        export_file = export_dir / "experience_export.json"

        data = []
        for item in items:
            data.append({
                "commit_sha": item.commit_sha,
                "commit_message": item.commit_message,
                "committed_at": item.committed_at.isoformat() if item.committed_at else None,
                "title": item.title,
                "steps": json.loads(item.steps_json) if item.steps_json else [],
                "candidate_files": json.loads(item.candidate_files_json) if item.candidate_files_json else [],
            })

        export_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._console.print(
            f"已导出 {len(data)} 条经验到 {export_file}", style="green"
        )

    # ===== Agent Turn =====

    async def _run_turn(self, text: str) -> None:
        target_session = self._session_id
        try:
            if not target_session:
                target_session = await self._runner.create_session(
                    user_id=self._user_id or "cli",
                    workspace_path=str(self._workspace),
                    llm_provider=self._llm_provider,
                    llm_model=self._llm_model,
                )
                self._session_id = target_session
            self._agent_busy = True
            self._ctrl_c_pending = False
            await self._runner.run_turn(target_session, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("agent turn failed")
            self._console.print(
                Panel(str(exc), title="Error", border_style="red", expand=False)
            )
        finally:
            self._agent_busy = False
            self._ctrl_c_pending = False
            self._print_status_line()
            queue = self._pending_for(target_session or "")
            if queue:
                next_text = queue.pop(0)
                self._console.print(f"[queued] Running next: {next_text[:60]}...", style="dim")
                asyncio.create_task(self._run_turn(next_text))

    # ===== 状态 =====

    def _print_status(self) -> None:
        pid = os.getpid()
        memory_mb = self._memory_usage_mb()
        state = self._runner.agent_state(self._session_id)
        session_short = self._session_id.split("_")[-1][:8] if self._session_id else "none"
        parts = []
        if memory_mb > 0:
            parts.append(f"{memory_mb:.1f}MB")
        parts.append(f"pid:{pid}")
        parts.append(f"session:{session_short}")
        parts.append(f"state:{state}")
        parts.append(f"model:{self._format_model()}")
        parts.append(f"workspace:{self._workspace}")
        self._console.print(" · ".join(parts), style="dim")

    @staticmethod
    def _memory_usage_mb() -> float:
        try:
            import psutil
            return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0

    # ===== Signal Handling =====

    def _install_signal_handler(self) -> None:
        def _handler(signum, frame):
            if self._agent_busy:
                if self._ctrl_c_pending:
                    self._should_exit = True
                    self._agent_busy = False
                    try:
                        asyncio.get_event_loop().call_soon_threadsafe(
                            lambda: asyncio.ensure_future(self._runner.stop_current(hard=True))
                        )
                    except Exception:
                        pass
                else:
                    self._ctrl_c_pending = True
                    try:
                        asyncio.get_event_loop().call_soon_threadsafe(
                            lambda: asyncio.ensure_future(self._runner.stop_current())
                        )
                    except Exception:
                        pass
            else:
                if self._ctrl_c_pending:
                    self._should_exit = True
                else:
                    self._ctrl_c_pending = True

        signal.signal(signal.SIGINT, _handler)


async def run_repl(
    *,
    session_id: Optional[str],
    workspace: Path,
    user_id: str,
    llm_provider: str = "",
    llm_model: str = "",
) -> None:
    """启动 Rich REPL。"""
    app = ReplApp(
        session_id=session_id,
        workspace=workspace,
        runner=AGENT_RUNNER,
        user_id=user_id,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    try:
        await app.run()
    except Exception:
        logging.exception("REPL 异常退出")
        raise
