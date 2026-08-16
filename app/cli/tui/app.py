from __future__ import annotations

import asyncio
import getpass
import json
import logging
import shlex
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Input, Markdown, OptionList, Static
from app.agents.internal_dispatch import set_internal_message_handler
from app.agents.output import OutboundMessage, OutboundMessageType, set_output_handler
from app.agents.sessions.manager import SESSION_MANAGER
from app.agents.sessions.message import Role
from app.config.settings import APP_VERSION
from app.cli.display_format import format_outbound_content
from app.cli.runner import AGENT_RUNNER, AgentRunner
from app.cli.tui.logo import MOMA_LOGO
from app.cli.tui.output_handler import OutputPresenter, PresenterAction, TuiOutputHandler
from app.cli.tui.session_picker import SessionPicker
from app.cli.tui.slash_commands import SLASH_COMMANDS
from app.cli.tui.slash_menu import SlashCommandMenu


TIPS = "Type / for commands, or ask a task"

# 运行中状态动画：Thinking=模型思考，Working=Agent 工具执行
_RUN_SPIN_FRAMES = ("|", "/", "-", "\\")


class MomaCoderApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "MOMA"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("ctrl+c", "interrupt_or_quit", "中断/退出", show=False, priority=True),
        Binding("ctrl+q", "request_quit", "退出", show=False),
        # priority=True：输入框聚焦时也能翻历史，不被 Input 吃掉
        Binding("pageup", "scroll_chat_up", "上翻历史", show=False, priority=True),
        Binding("pagedown", "scroll_chat_down", "下翻历史", show=False, priority=True),
        Binding("ctrl+up", "scroll_chat_up", "上翻历史", show=False, priority=True),
        Binding("ctrl+down", "scroll_chat_down", "下翻历史", show=False, priority=True),
        # 斜杠菜单 / 会话选择器导航：priority=True 保证 Input 聚焦时也能收到
        # 不绑 tab：Screen 默认 tab→focus_next，绑了会破坏焦点切换
        Binding("up", "menu_up", show=False, priority=True),
        Binding("down", "menu_down", show=False, priority=True),
        Binding("escape", "menu_escape", show=False, priority=True),
    ]

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
        super().__init__()
        # session_id 可为 None：用户发第一条消息时才在 _run_turn 里调 create_session
        self._session_id = session_id
        self._workspace = workspace
        self._runner = runner
        self._user_id = user_id
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._presenter = OutputPresenter()
        self._agent_busy = False
        # 用户消息排队：A 运行中再提交 B/C，进队列等当前 turn 结束（含被 Ctrl+C 停止）后自动跑
        # 按 session 隔离：切到别的 session 时，原 session 的队列留给切回时处理
        self._pending_queues: dict[str, list[str]] = {}
        # 工具行 widget 按 tool_call_id 索引：tool_call 到达时挂载 ▶，tool_result 到达时刷新为 ✓/✗
        self._tool_line_widgets: dict[str, "Static"] = {}
        self._ctrl_c_armed = False  # 运行中首次 Ctrl+C 武装；再按则退出
        self._last_ctrl_c_at = 0.0  # Ctrl+C：gap<50ms 视为按键重复；50~200ms 视为双击触发中断/退出
        self._current_stream_body: str = ""  # 当前流式预览缓冲区内容（供 Ctrl+C 复制）
        self._last_assistant_text: str = ""  # 最后一条助手消息文本（供 Ctrl+C 复制）
        self._last_assistant_md: Optional[Markdown] = None  # 当前最新 assistant Markdown widget，用于降级
        self._has_chat = False
        self._status_timer: Optional[asyncio.Task[None]] = None
        self._ui_lock = asyncio.Lock()
        # 历史消息缓存：切 session 时填充，翻页/搜索用
        self._history_messages: list = []
        self._crash_log = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "tui_crash.log"
        # 流式直播用 Static + 节流：避免半截 Markdown 在 Windows 上把 Textual 打崩
        self._pending_live: Optional[str] = None
        self._live_timer: Optional[Timer] = None
        self._last_live_flush = 0.0
        self._live_min_interval = 0.08
        self._run_started_at: Optional[float] = None
        self._run_spin_idx = 0
        self._run_spin_timer: Optional[Timer] = None
        self._run_phase = "Thinking"
        # CodeBase 状态面板：常驻 welcome-box 右侧，替代 Recent activity
        self._codebase_poll_task: Optional[asyncio.Task[None]] = None
        self._codebase_repo_id: Optional[str] = None
        self._codebase_panel_active = False
        self._codebase_last_indicator: str = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="main-layout"):
            with Container(id="welcome-box"):
                with Horizontal(id="welcome-inner"):
                    with Vertical(id="welcome-left"):
                        yield Static("Welcome back!", classes="welcome-greeting")
                        with Horizontal(id="welcome-brand-row"):
                            yield Static(MOMA_LOGO, classes="mascot-block", id="moma-logo")
                            with Vertical(id="welcome-text-col"):
                                yield Static(
                                    f"MOMA v{APP_VERSION}",
                                    classes="brand-line",
                                    id="brand-line",
                                )
                                yield Static("", classes="meta-line", id="model-line")
                                yield Static("", classes="meta-line-dim", id="workspace-line")
                    with Vertical(id="welcome-right"):
                        yield Static("Tips for getting started", classes="panel-title")
                        yield Static(TIPS, classes="panel-body", id="tips-body")
                        yield Static("Recent activity", classes="panel-title recent-title", id="recent-title")
                        yield Static("", classes="panel-body-dim", id="recent-body")
            with VerticalScroll(id="chat-scroll"):
                yield Static("", id="chat-spacer")
                # 直播区必须用 Static：流式过程中 Markdown 常遇到未闭合 ** / ``` 导致 Windows 控制台致命退出
                yield Static("", id="stream-live")
            yield Static("", id="run-status")
            yield SlashCommandMenu()
            yield SessionPicker()
            with Horizontal(id="prompt-row"):
                yield Static(">", classes="prompt-glyph")
                yield Input(
                    id="prompt-input",
                    placeholder='Type / for commands, or ask a question',
                )
            with Horizontal(id="status-row"):
                yield Static("", id="status-left")
                yield Static("", id="status-right")

    async def on_mount(self) -> None:
        set_output_handler(TuiOutputHandler(self))
        set_internal_message_handler(self._runner.handle_internal_message)
        # 拦截 Screen 的鼠标滚轮处理，直接路由到 #chat-scroll
        _app = self

        def _patched_scroll_up(event: events.MouseScrollUp) -> None:
            logging.info("[screen-scroll] UP intercepted by patched handler")
            try:
                scroll = _app.query_one("#chat-scroll", VerticalScroll)
                scroll.scroll_relative(y=-3, animate=False)
                event.stop()
            except Exception:
                pass

        def _patched_scroll_down(event: events.MouseScrollDown) -> None:
            logging.info("[screen-scroll] DOWN intercepted by patched handler")
            try:
                scroll = _app.query_one("#chat-scroll", VerticalScroll)
                scroll.scroll_relative(y=3, animate=False)
                event.stop()
            except Exception:
                pass

        self.screen._on_mouse_scroll_up = _patched_scroll_up
        self.screen._on_mouse_scroll_down = _patched_scroll_down
        self._refresh_header()
        await self._refresh_recent()
        self._refresh_status()
        self.query_one("#chat-scroll", VerticalScroll).display = False
        self.query_one("#stream-live", Static).display = False
        self.query_one("#run-status", Static).display = False
        self.query_one("#prompt-input", Input).focus()
        self._status_timer = asyncio.create_task(self._status_loop())
        self._codebase_poll_task = asyncio.create_task(self._codebase_status_loop())

    async def on_unmount(self) -> None:
        self._stop_run_status()
        if self._status_timer is not None:
            self._status_timer.cancel()
            try:
                await self._status_timer
            except asyncio.CancelledError:
                pass
        if self._codebase_poll_task is not None:
            self._codebase_poll_task.cancel()
            try:
                await self._codebase_poll_task
            except asyncio.CancelledError:
                pass

    def _pending_for(self, session_id: str) -> list[str]:
        """返回指定 session 的待发队列（不存在则创建空列表）。"""
        return self._pending_queues.setdefault(session_id, [])

    def _release_input(self) -> None:
        self._agent_busy = False
        self._ctrl_c_armed = False
        self._stop_run_status()
        prompt = self.query_one("#prompt-input", Input)
        prompt.disabled = False
        prompt.focus()
        self._refresh_status()
        # 诊断：任务结束时记录滚动状态
        try:
            scroll = self.query_one("#chat-scroll", VerticalScroll)
            children = list(scroll.children)
            logging.info(
                "[scroll-release] task done: scroll_y=%.1f max_y=%.1f children=%d",
                float(scroll.scroll_y), float(scroll.max_scroll_y), len(children),
            )
        except Exception:
            pass

    def _start_run_status(self) -> None:
        """任务开始：在输入框上方显示持续动画，直到结束。"""
        self._run_started_at = time.monotonic()
        self._run_spin_idx = 0
        self._run_phase = "Thinking"
        widget = self.query_one("#run-status", Static)
        widget.display = True
        self._tick_run_status()
        if self._run_spin_timer is not None:
            self._run_spin_timer.stop()
        self._run_spin_timer = self.set_interval(0.1, self._tick_run_status)

    def _stop_run_status(self) -> None:
        if self._run_spin_timer is not None:
            self._run_spin_timer.stop()
            self._run_spin_timer = None
        self._run_started_at = None
        self._run_spin_idx = 0
        self._run_phase = "Thinking"
        try:
            widget = self.query_one("#run-status", Static)
            widget.update("")
            widget.display = False
        except Exception:
            pass

    def _set_run_phase(self, phase: str) -> None:
        if phase not in ("Thinking", "Working"):
            return
        if self._run_phase == phase:
            return
        self._run_phase = phase
        self._tick_run_status()

    def _tick_run_status(self) -> None:
        if not self._agent_busy or self._run_started_at is None:
            return
        elapsed = max(0, int(time.monotonic() - self._run_started_at))
        frame = _RUN_SPIN_FRAMES[self._run_spin_idx % len(_RUN_SPIN_FRAMES)]
        self._run_spin_idx += 1
        self.query_one("#run-status", Static).update(
            f"{frame} {self._run_phase}... ({elapsed}s)  ·  esc to interrupt"
        )

    def _ensure_chat_visible(self) -> VerticalScroll:
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        if not self._has_chat:
            self._has_chat = True
            scroll.display = True
        return scroll

    async def _wipe_chat_view(self) -> None:
        """清空聊天区消息，保留 spacer 与 stream-live。"""
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        keep = {"chat-spacer", "stream-live"}
        to_remove = [child for child in list(scroll.children) if child.id not in keep]
        for child in to_remove:
            await child.remove()
        self._has_chat = False
        # 工具行 widget 已随子节点移除，索引同步清空
        self._tool_line_widgets.clear()
        # 先不要 display=False：部分 Textual 版本在空滚动区隐藏后会让后续 query 不稳定
        await self._clear_stream()

    # 滑动窗口：chat 区最多保留的 widget 数量（工具消息不计入，≈30 条用户/助手消息）
    _CHAT_WIDGET_LIMIT = 50

    async def _mount_chat_widget(self, widget) -> VerticalScroll:
        """消息插在 stream-live 之前，保证直播区始终在滚动区底部。"""
        scroll = self._ensure_chat_visible()
        stick_to_bottom = self._is_chat_at_bottom(scroll)
        try:
            live = self.query_one("#stream-live", Static)
            await scroll.mount(widget, before=live)
        except Exception:
            await scroll.mount(widget)
        # 裁剪旧 widget（只在超限时执行，减少滚动跳动）
        await self._trim_chat_widgets(scroll)
        if stick_to_bottom:
            scroll.call_later(scroll.scroll_end, animate=False)
        return scroll

    # 工具消息 CSS 类名前缀，不计入滑动窗口（单行 Static，渲染成本极低）
    _TOOL_MSG_CLASSES = {"tool-msg"}

    async def _trim_chat_widgets(self, scroll: VerticalScroll) -> None:
        """滑动窗口：超过上限时移除最旧 widget。工具消息不计入。"""
        try:
            live = self.query_one("#stream-live", Static)
        except Exception:
            return
        children = list(scroll.children)
        msg_children = [c for c in children if c is not live]
        heavy = [c for c in msg_children if not self._is_lightweight_widget(c)]
        excess = len(heavy) - self._CHAT_WIDGET_LIMIT
        if excess <= 0:
            return
        logging.info(
            "[scroll-trim] removing %d widgets (heavy=%d limit=%d) before: scroll_y=%.1f max_y=%.1f children=%d",
            excess, len(heavy), self._CHAT_WIDGET_LIMIT,
            float(scroll.scroll_y), float(scroll.max_scroll_y), len(children),
        )
        for w in msg_children:
            if excess <= 0:
                break
            if self._is_lightweight_widget(w):
                continue
            try:
                await w.remove()
                excess -= 1
            except Exception:
                pass
        logging.info(
            "[scroll-trim] after: scroll_y=%.1f max_y=%.1f children=%d",
            float(scroll.scroll_y), float(scroll.max_scroll_y), len(list(scroll.children)),
        )

    @staticmethod
    def _is_lightweight_widget(widget) -> bool:
        """判断是否为轻量 widget（工具消息、assistant-label 等），不计入滑动窗口。"""
        try:
            classes = set((widget.classes or "").split())
            return bool(classes & MomaCoderApp._TOOL_MSG_CLASSES)
        except Exception:
            return False

    @staticmethod
    def _is_chat_at_bottom(scroll: VerticalScroll, threshold: int = 2) -> bool:
        """判断 chat-scroll 是否贴底（允许 threshold 行容差）。"""
        try:
            max_y = scroll.max_scroll_y
            return max_y <= 0 or scroll.scroll_y >= max_y - threshold
        except Exception:
            return True

    async def _clear_chat(self) -> None:
        await self._wipe_chat_view()
        await self._append_plain("Chat cleared.", classes="system-msg")

    def handle_agent_output(self, msg: OutboundMessage) -> None:
        # 后台 session 的输出不渲染到当前 UI；其消息已通过 push_history_message 落库，
        # 切回该 session 时 _render_session_preview 会回放最近可见消息。
        if msg.session_id != self._session_id:
            return
        self._ensure_chat_visible()
        actions = self._presenter.feed(msg)
        if not actions:
            # 无动作也要检查 RUN_END（如纯 STREAM_END body 为空时）
            if msg.outbound_type == OutboundMessageType.RUN_END and not self._pending_for(self._session_id):
                self._release_input()
            return

        # live 动作立即处理（节流刷新流式预览）；其余动作进 worker 顺序执行
        deferred: list[PresenterAction] = []
        for action in actions:
            if action.kind == "live":
                self._set_run_phase("Thinking")
                self._schedule_live(action.text)
            elif action.kind != "none":
                deferred.append(action)

        if deferred:
            self._cancel_live_timer()

            # 捕获当前 session_id：worker 运行期间若用户切换了 session，剩余 action 直接丢弃，
            # 否则旧 session 的 commit/tool 行会挂到新 session 的聊天区顶部（_wipe 在 await 间隙被穿插）。
            scheduled_session = self._session_id

            async def _run_deferred() -> None:
                for a in deferred:
                    if self._session_id != scheduled_session:
                        return
                    # 每个动作前刷新 phase：commit=Thinking, tool/ask/subagent=Working
                    if a.kind == "commit":
                        self._set_run_phase("Thinking")
                    elif a.kind in ("tool", "ask", "subagent"):
                        self._set_run_phase("Working")
                    await self._apply_action(a)

            self.run_worker(
                _run_deferred(),
                exclusive=False,
                group="chat-ui",
                exit_on_error=False,
            )

        last = actions[-1]
        # RUN_END 释放输入：但有排队消息时不释放（_run_turn finally 会接跑下一条）
        if (msg.outbound_type == OutboundMessageType.RUN_END or last.finished) and not self._pending_for(self._session_id):
            self._release_input()

    def _schedule_live(self, text: str) -> None:
        self._pending_live = text
        now = time.monotonic()
        if now - self._last_live_flush >= self._live_min_interval:
            self._flush_live()
            return
        if self._live_timer is None:
            delay = max(0.01, self._live_min_interval - (now - self._last_live_flush))
            self._live_timer = self.set_timer(delay, self._flush_live)

    def _cancel_live_timer(self) -> None:
        if self._live_timer is not None:
            self._live_timer.stop()
            self._live_timer = None
        if self._pending_live is not None:
            self._flush_live()

    def _flush_live(self) -> None:
        self._live_timer = None
        text = self._pending_live
        if text is None:
            return
        self._pending_live = None
        self._last_live_flush = time.monotonic()
        # 捕获 session_id：worker 真正运行时若已切走，旧 session 的流式文本不该写到新 session 的 stream-live
        scheduled_session = self._session_id
        self.run_worker(
            self._on_stream_live(text, scheduled_session),
            exclusive=True,
            group="stream-live",
            exit_on_error=False,
        )

    async def _on_stream_live(self, text: str, scheduled_session: Optional[str] = None) -> None:
        try:
            # 进入 worker 时再校验一次：等待 _ui_lock 期间可能已切 session
            if scheduled_session is not None and self._session_id != scheduled_session:
                return
            async with self._ui_lock:
                if scheduled_session is not None and self._session_id != scheduled_session:
                    return
                await self._set_stream_live(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("stream live update failed: %s", exc)

    async def _apply_action(self, action: PresenterAction) -> None:
        async with self._ui_lock:
            try:
                logging.info(
                    "apply_action: kind=%s text_len=%d",
                    action.kind, len(action.text or ""),
                )
                if action.kind == "none":
                    return
                if action.kind == "live":
                    await self._set_stream_live(action.text)
                    return
                if action.kind == "commit":
                    await self._clear_stream()
                    await self._append_assistant_markdown(action.text)
                    return
                if action.kind == "tool":
                    await self._append_tool_line(action.text, action.success, action.tool_call_id)
                    return
                if action.kind == "ask":
                    await self._append_plain(f"[ask] {action.text}", classes="system-msg")
                    return
                if action.kind == "subagent":
                    await self._append_plain(f"[sub] {action.text}", classes="subagent-msg")
                    return
                if action.kind == "plain":
                    await self._append_assistant_markdown(action.text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.exception("chat UI action failed kind=%s", action.kind)
                try:
                    await self._append_plain(f"UI render error: {exc}", classes="error-msg")
                except Exception:
                    pass

    async def _set_stream_live(self, body: str) -> None:
        """流式预览：纯文本 Static，绝不走 Markdown 解析。"""
        live = self.query_one("#stream-live", Static)
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        # 更新前先记贴底状态：布局未刷新前 _is_chat_at_bottom 仍反映旧视图，正好用来判断用户意图
        stick_to_bottom = self._is_chat_at_bottom(scroll)
        live.display = True
        content = body if body else "..."
        self._current_stream_body = body  # 追踪缓冲区内容，供 Ctrl+C 复制
        # 诊断日志：定位"中途卡住"根因
        try:
            logging.info(
                "stream_live pre: len=%d scroll_y=%.1f max_y=%.1f children=%d stick=%s",
                len(content), float(scroll.scroll_y), float(scroll.max_scroll_y),
                len(scroll.children), stick_to_bottom,
            )
        except Exception:
            pass
        # 用 Rich Text 禁用 markup，避免 [..] / 半截 ** 触发解析崩溃
        live.update(Text(f"MOMA\n\n{content}"))
        # 贴底时跟随流式输出滚动；用户向上翻历史时不打扰
        # 用 call_later(0.05) 等一个事件循环 tick 让布局更新，比 call_after_refresh 更可靠
        if stick_to_bottom:
            scroll.call_later(scroll.scroll_end, animate=False)

    async def _clear_stream(self) -> None:
        live = self.query_one("#stream-live", Static)
        live.update(Text(""))
        live.display = False
        self._current_stream_body = ""  # 流式结束，清空缓冲区追踪

    async def _append_assistant_markdown(self, body: str) -> None:
        # 先将上一条 assistant Markdown 降级为 Static，保证窗口内只有一条 MD
        await self._demote_last_markdown()
        await self._mount_chat_widget(Static("MOMA", classes="assistant-label"))
        self._last_assistant_text = body
        try:
            widget = Markdown(body or "", classes="assistant-md")
            self._last_assistant_md = widget
            await self._mount_chat_widget(widget)
        except Exception:
            self._last_assistant_md = None
            await self._mount_chat_widget(Static(Text(body or ""), classes="assistant-md"))

    async def _demote_last_markdown(self) -> None:
        """将上一条 assistant Markdown 降级为 Static(Text)，减少渲染开销。"""
        md = self._last_assistant_md
        if md is None:
            return
        self._last_assistant_md = None
        try:
            if not md.is_attached:
                return
            scroll = self.query_one("#chat-scroll", VerticalScroll)
            old_scroll_y = scroll.scroll_y
            old_max = scroll.max_scroll_y
            md_text = md.export()
            new_widget = Static(Text(md_text), classes="assistant-md")
            await md.replace(new_widget)
            logging.info(
                "[scroll-demote] MD→Static: old_scroll_y=%.1f old_max=%.1f new_max=%.1f children=%d",
                old_scroll_y, old_max, float(scroll.max_scroll_y), len(list(scroll.children)),
            )
        except Exception:
            pass

    async def _append_plain(self, text: str, *, classes: str = "system-msg") -> None:
        # 用 Text 禁用 markup 解析：工具结果/异常消息里含 [field=True,\n...] 会让 textual 崩
        await self._mount_chat_widget(Static(Text(text), classes=classes))

    async def _append_tool_line(
        self, text: str, success: Optional[bool], tool_call_id: Optional[str | list[str]] = None
    ) -> None:
        """工具调用单行显示：行首彩色图标 + tool_name(params)。

        - success=True  -> ✓ 绿色
        - success=False -> ✗ 红色
        - success=None  -> ▶ 灰色（tool_call 执行中或无 success 标志）

        若 tool_call_id 匹配已挂载的 ▶ 行（tool_call 先到、tool_result 后到），
        则原地刷新图标与文本，不再追加新行。
        tool_call_id 可以是单个 str（tool_result）或 list[str]（tool_call 含多个工具）。
        """
        if success is True:
            icon, color = "✓", "#22c55e"
        elif success is False:
            icon, color = "✗", "#ef4444"
        else:
            icon, color = "▶", "#9a9590"
        line = Text()
        line.append(icon, style=color)
        line.append(" ")
        line.append(text)

        # 统一为列表处理
        ids: list[str] = []
        if isinstance(tool_call_id, list):
            ids = [i for i in tool_call_id if i]
        elif isinstance(tool_call_id, str) and tool_call_id:
            ids = [tool_call_id]

        # 检查是否有已挂载的 widget 需要更新（tool_result 匹配 tool_call 创建的 ▶）
        for tid in ids:
            if tid in self._tool_line_widgets:
                self._tool_line_widgets[tid].update(line)
                return

        # 无匹配：为每个 tool_call_id 创建独立 widget
        widget = Static(line, classes="tool-msg")
        for tid in ids:
            self._tool_line_widgets[tid] = widget
        await self._mount_chat_widget(widget)

    async def _append_user(self, text: str) -> None:
        await self._mount_chat_widget(Static(Text(f"You: {text}"), classes="user-msg"))

    def action_scroll_chat_up(self) -> None:
        self._scroll_chat_pages(-1)

    def action_scroll_chat_down(self) -> None:
        self._scroll_chat_pages(1)

    def _scroll_chat_pages(self, direction: int) -> None:
        try:
            scroll = self.query_one("#chat-scroll", VerticalScroll)
        except Exception:
            return
        if not scroll.display:
            return
        if direction < 0:
            scroll.scroll_page_up(animate=False)
        else:
            scroll.scroll_page_down(animate=False)

    def _scroll_chat_lines(self, lines: int) -> None:
        try:
            scroll = self.query_one("#chat-scroll", VerticalScroll)
        except Exception:
            return
        if not scroll.display:
            return
        old_y = scroll.scroll_y
        scroll.scroll_relative(y=lines, animate=False)
        logging.info(
            "[scroll-lines] lines=%d before=%.1f after=%.1f max=%.1f",
            lines, old_y, float(scroll.scroll_y), float(scroll.max_scroll_y),
        )

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._menus_open():
            return
        try:
            scroll = self.query_one("#chat-scroll", VerticalScroll)
            logging.info(
                "[scroll-mouse] UP: before scroll_y=%.1f max_y=%.1f display=%s",
                float(scroll.scroll_y), float(scroll.max_scroll_y), scroll.display,
            )
        except Exception:
            pass
        self._scroll_chat_lines(-3)
        event.stop()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._menus_open():
            return
        try:
            scroll = self.query_one("#chat-scroll", VerticalScroll)
            logging.info(
                "[scroll-mouse] DOWN: before scroll_y=%.1f max_y=%.1f display=%s",
                float(scroll.scroll_y), float(scroll.max_scroll_y), scroll.display,
            )
        except Exception:
            pass
        self._scroll_chat_lines(3)
        event.stop()

    def _menus_open(self) -> bool:
        try:
            if self.query_one("#session-picker", SessionPicker).is_open:
                return True
            if self.query_one("#slash-menu", SlashCommandMenu).is_open:
                return True
        except Exception:
            return False
        return False

    def _refresh_header(self) -> None:
        workspace_text = str(self._workspace)
        if len(workspace_text) > 64:
            workspace_text = "..." + workspace_text[-63:]
        self.query_one("#workspace-line", Static).update(Text(workspace_text))
        self.query_one("#model-line", Static).update(Text(self._format_model()))

    def _format_model(self) -> str:
        provider = (self._llm_provider or "").strip()
        model = (self._llm_model or "").strip()
        if provider and model:
            return f"{provider} · {model}"
        if model:
            return model
        return "default model · local workspace"

    async def _refresh_recent(self) -> None:
        widget = self.query_one("#recent-body", Static)
        try:
            sessions = await SESSION_MANAGER.get_all_sessions(
                channel_type="cli",
                user_id=self._user_id,
            )
        except Exception:
            widget.update(Text("No recent activity"))
            return
        visible = [s for s in sessions if not getattr(s, "is_internal", False)]
        visible.sort(
            key=lambda s: getattr(s, "last_updated", None) or datetime.min,
            reverse=True,
        )
        lines: list[str] = []
        for session in visible[:2]:
            sid = session.session_id
            stamp = ""
            last_updated = getattr(session, "last_updated", None)
            if last_updated is not None:
                stamp = last_updated.strftime("%m-%d %H:%M")
            label = session.display_title(36)
            marker = " >" if sid == self._session_id else ""
            lines.append(f"{stamp}  {label}{marker}" if stamp else f"{label}{marker}")
        widget.update(Text("\n".join(lines) if lines else "No recent activity"))

    def _refresh_status(self) -> None:
        pid = os.getpid()
        memory_mb = self._memory_usage_mb()
        state = self._runner.agent_state(self._session_id)
        session_short = self._session_id.split("_")[-1][:8] if self._session_id else "none"
        if memory_mb > 0:
            left = f"{memory_mb:.1f}MB · pid:{pid} · {session_short}"
        else:
            left = f"pid:{pid} · {session_short}"
        # 扫描运行时追加 CB 指示器（底部 status-row 仍可见）
        if self._codebase_last_indicator:
            left = f"{left} · {self._codebase_last_indicator}"
        if self._agent_busy:
            queue_n = len(self._pending_for(self._session_id))
            if queue_n > 0:
                right = f"* running · {queue_n} queued · esc to interrupt"
            else:
                right = "* running · esc to interrupt"
            right_class = "status-running"
        else:
            right = f"* {state.lower()}"
            right_class = "status-idle"
        self.query_one("#status-left", Static).update(left)
        status_right = self.query_one("#status-right", Static)
        status_right.update(right)
        status_right.remove_class("status-running", "status-idle")
        status_right.add_class(right_class)

    @staticmethod
    def _memory_usage_mb() -> float:
        try:
            import psutil
            return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            self._refresh_status()

    # ===== CodeBase 状态面板 =====

    async def _handle_codebase_command(self, arg: str) -> None:
        """`/codebase [status|rescan|clean|experience|experience export]`：CodeBase 子命令。"""
        from app.codebase.integration.status_formatter import format_codebase_status

        # 无参数 → 显示帮助
        if not arg:
            help_text = (
                "CodeBase 子命令:\n"
                "  /codebase status                       查看分析进度（默认）\n"
                "  /codebase rescan                       增量重新扫描（仅处理有变更的文件）\n"
                "  /codebase clean                        清理所有分析数据（状态+向量+图谱）\n"
                "  /codebase clean --rescan               清理后立即重新扫描分析\n"
                "  /codebase locate <query> [top_k]       符号定位（语义搜索）\n"
                "  /codebase similar <code_text> [top_k]  相似代码查询（支持反引号包裹多行代码）\n"
                "  /codebase query file <path> <dependents|depended>\n"
                "  /codebase query symbol <name> <callers|callees> [limit]\n"
                "                                         依赖/调用关系查询\n"
                "  /codebase experience                   查看 MR 经验提取状态\n"
                "  /codebase experience search <query>    搜索历史经验\n"
                "  /codebase experience export            导出经验到 JSON 文件"
            )
            await self._append_plain(help_text, classes="system-msg")
            return

        # /codebase experience [export]
        if arg.startswith("experience"):
            sub_arg = arg[len("experience"):].strip()
            await self._handle_experience_command(sub_arg)
            return

        if arg == "rescan":
            from app.codebase.integration.orchestrator import AutoAnalyzeOrchestrator
            try:
                await AutoAnalyzeOrchestrator.trigger_reanalyze(
                    workspace_path=str(self._workspace),
                    user_id=self._user_id,
                )
            except Exception as exc:
                await self._append_plain(f"触发失败: {exc}", classes="error-msg")
                return
            await self._append_plain("已触发 CodeBase 重新扫描。", classes="system-msg")
            # 立刻刷一次面板，让用户看到 running 状态
            try:
                await self._refresh_codebase_panel()
            except Exception:
                pass

        if arg.startswith("clean"):
            do_rescan = "--rescan" in arg
            await self._handle_codebase_clean(do_rescan=do_rescan)
            return

        if arg.startswith("locate"):
            await self._handle_codebase_locate(arg[len("locate"):].strip())
            return

        if arg.startswith("similar"):
            await self._handle_codebase_similar(arg[len("similar"):].strip())
            return

        if arg.startswith("query"):
            await self._handle_codebase_query(arg[len("query"):].strip())
            return

        # status 或未知参数 → 默认显示状态
        try:
            text = await format_codebase_status(str(self._workspace), self._user_id)
        except Exception as exc:
            await self._append_plain(f"读取 CodeBase 状态失败: {exc}", classes="error-msg")
            return
        await self._append_plain(text, classes="system-msg")

    async def _codebase_status_loop(self) -> None:
        """每 5s 刷一次 CodeBase 状态；Agent 运行期间跳过，避免抢事件循环。"""
        while True:
            await asyncio.sleep(30)
            if self._agent_busy:
                continue
            try:
                await self._refresh_codebase_panel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.debug("codebase panel refresh failed: %s", exc)

    async def _refresh_codebase_panel(self) -> None:
        from app.codebase.repo_analysis.services.analysis_service import AnalysisService
        from app.codebase.repo_mgmt.services.repo_resolver import RepoResolver
        from app.codebase.integration.status_formatter import (
            format_codebase_panel_text,
            format_codebase_status_indicator,
        )
        from app.infrastructure.database import get_db_session

        # 解析并缓存 repo_id（找不到就静默返回，不显示面板）
        if self._codebase_repo_id is None:
            async with get_db_session() as db:
                repo = await RepoResolver.get_by_path(db, str(self._workspace))
                if repo is None:
                    return
                self._codebase_repo_id = repo.id

        scan = await AnalysisService.get_scan_status(repo_id=self._codebase_repo_id)
        summary = await AnalysisService.get_summary(repo_id=self._codebase_repo_id)

        # 更新底部 status-row 指示器缓存（_refresh_status 会读）
        self._codebase_last_indicator = format_codebase_status_indicator(scan, summary)
        self._refresh_status()

        text = format_codebase_panel_text(scan, summary)
        await self._show_codebase_panel(text)

    async def _show_codebase_panel(self, text: str) -> None:
        try:
            title = self.query_one("#recent-title", Static)
            body = self.query_one("#recent-body", Static)
        except Exception:
            return
        if not self._codebase_panel_active:
            title.update(Text("CodeBase"))
            self._codebase_panel_active = True
        body.update(Text(text))

    # ===== CodeBase Query Tools =====

    @staticmethod
    def _extract_backtick_text(arg: str) -> str:
        """从参数中提取反引号包裹的多行文本。如果没有反引号，原样返回。"""
        stripped = arg.strip()
        if stripped.startswith("`"):
            idx = stripped.find("`", 1)
            if idx > 0:
                return stripped[1:idx]
        return arg

    async def _get_repo_id(self) -> Optional[str]:
        """获取当前 workspace 对应的 repo_id。"""
        from app.codebase.repo_mgmt.services.repo_resolver import RepoResolver
        from app.infrastructure.database import get_db_session
        async with get_db_session() as db:
            repo = await RepoResolver.get_by_path(db, str(self._workspace))
            return repo.id if repo else None

    async def _handle_codebase_locate(self, arg: str) -> None:
        """`/codebase locate <query> [top_k]`：符号/关键词语义定位。"""
        try:
            parts = shlex.split(arg, posix=False)
        except ValueError:
            parts = arg.split()
        if not parts:
            await self._append_plain("用法: /codebase locate <query> [top_k]", classes="system-msg")
            return
        query = parts[0]
        top_k = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10

        repo_id = await self._get_repo_id()
        if not repo_id:
            await self._append_plain("未找到当前工作区对应的仓库", classes="error-msg")
            return

        from app.codebase.integration.facade import CodebaseFacade
        try:
            result = await CodebaseFacade.locate_symbol(repo_id=repo_id, query=query, top_k=top_k)
            await self._append_plain(f"/codebase locate {query} {top_k}\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str), classes="system-msg")
        except Exception as exc:
            await self._append_plain(f"符号定位失败: {exc}", classes="error-msg")

    async def _handle_codebase_similar(self, arg: str) -> None:
        """`/codebase similar <code_text> [top_k]`：相似代码查询。支持反引号包裹多行代码。"""
        if not arg:
            await self._append_plain("用法: /codebase similar <code_text> [top_k]\n支持反引号包裹多行代码，如:\n  /codebase similar `def foo():\n    return 1` 5", classes="system-msg")
            return

        top_k = 10
        stripped = arg.strip()
        if stripped.startswith("`"):
            idx = stripped.find("`", 1)
            if idx > 0:
                code_text = stripped[1:idx]
                rest = stripped[idx + 1:].strip()
                if rest.isdigit():
                    top_k = int(rest)
            else:
                code_text = stripped[1:]
        else:
            parts = arg.rsplit(maxsplit=1)
            code_text = parts[0]
            top_k = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10

        repo_id = await self._get_repo_id()
        if not repo_id:
            await self._append_plain("未找到当前工作区对应的仓库", classes="error-msg")
            return

        from app.codebase.integration.facade import CodebaseFacade
        try:
            result = await CodebaseFacade.search_similar_code(repo_id=repo_id, code_text=code_text, top_k=top_k)
            preview = code_text if len(code_text) <= 50 else code_text[:47] + "..."
            await self._append_plain(f"/codebase similar `{preview}` {top_k}\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str), classes="system-msg")
        except Exception as exc:
            await self._append_plain(f"相似代码查询失败: {exc}", classes="error-msg")

    async def _handle_codebase_query(self, arg: str) -> None:
        """`/codebase query <file|symbol> <target> <direction> [limit]`：依赖/调用关系查询。"""
        try:
            parts = shlex.split(arg, posix=False)
        except ValueError:
            parts = arg.split()
        if len(parts) < 3:
            await self._append_plain(
                "用法:\n"
                "  /codebase query file <path> <dependents|depended>\n"
                "  /codebase query symbol <symbol_name> <callers|callees> [limit]\n"
                "\n"
                "示例:\n"
                "  /codebase query symbol ask_tools_stream callers\n"
                "  /codebase query file src/main.py dependents",
                classes="system-msg",
            )
            return
        target_type = parts[0]
        target = parts[1]
        direction = parts[2]
        limit = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 20

        repo_id = await self._get_repo_id()
        if not repo_id:
            await self._append_plain("未找到当前工作区对应的仓库", classes="error-msg")
            return

        from app.codebase.integration.facade import CodebaseFacade
        try:
            result = await CodebaseFacade.query_dependencies(
                repo_id=repo_id,
                target_type=target_type,
                target=target,
                direction=direction,
                limit=limit,
            )
            cmd = f"/codebase query {target_type} {target} {direction} {limit}"
            await self._append_plain(cmd + "\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str), classes="system-msg")
        except Exception as exc:
            await self._append_plain(f"依赖查询失败: {exc}", classes="error-msg")

    # ===== CodeBase Experience Search =====

    async def _handle_experience_search(self, query: str) -> None:
        """按自然语言查询 MR 经验。"""
        repo_id = await self._get_repo_id()
        if not repo_id:
            await self._append_plain("未找到当前工作区对应的仓库", classes="error-msg")
            return
        from app.codebase.integration.facade import CodebaseFacade
        try:
            result = await CodebaseFacade.search_patterns(repo_id=repo_id, query=query, top_k=10)
            await self._append_plain(f"/codebase experience search {query}\n" + json.dumps(result, ensure_ascii=False, indent=2, default=str), classes="system-msg")
        except Exception as exc:
            await self._append_plain(f"经验查询失败: {exc}", classes="error-msg")

    # ===== CodeBase Clean =====

    async def _handle_codebase_clean(self, do_rescan: bool = False) -> None:
        """清理指定仓库的全部分析数据，可选清理后立即重分析。"""
        from app.codebase.repo_analysis.services.analysis_service import AnalysisService
        from app.codebase.repo_mgmt.services.repo_resolver import RepoResolver
        from app.infrastructure.database import get_db_session

        async with get_db_session() as db:
            repo = await RepoResolver.get_by_path(db, str(self._workspace))
            if not repo:
                await self._append_plain("未找到当前工作区对应的仓库，请先执行 /codebase rescan。", classes="error-msg")
                return
            repo_id = repo.id

        await self._append_plain(f"正在清理仓库分析数据 (repo_id={repo_id[:8]}...)...", classes="system-msg")
        try:
            await AnalysisService.delete_repo_analysis_data(repo_id)
        except Exception as exc:
            await self._append_plain(f"清理失败: {exc}", classes="error-msg")
            return

        await self._append_plain("清理完成：状态记录、向量数据、图谱数据已全部删除。", classes="system-msg")

        if do_rescan:
            await self._append_plain("正在启动重新扫描分析...", classes="system-msg")
            from app.codebase.integration.orchestrator import AutoAnalyzeOrchestrator
            try:
                await AutoAnalyzeOrchestrator.trigger_reanalyze(
                    workspace_path=str(self._workspace),
                    user_id=self._user_id,
                )
            except Exception as exc:
                await self._append_plain(f"触发重分析失败: {exc}", classes="error-msg")
                return
            await self._append_plain("已触发全量重新分析。", classes="system-msg")

        try:
            await self._refresh_codebase_panel()
        except Exception:
            pass

    # ===== Experience 经验提取 =====

    async def _handle_experience_command(self, arg: str) -> None:
        """`/codebase experience [search <query> | export]`：经验状态/搜索/导出。"""
        arg = arg.strip()
        if arg == "export":
            await self._export_experience()
            return
        if arg.startswith("search"):
            query = arg[len("search"):].strip()
            if not query:
                await self._append_plain("用法: /codebase experience search <query>", classes="system-msg")
                return
            await self._handle_experience_search(query)
            return
        from app.codebase.integration.status_formatter import format_experience_status
        try:
            text = await format_experience_status(str(self._workspace))
        except Exception as exc:
            await self._append_plain(f"读取经验提取状态失败: {exc}", classes="error-msg")
            return
        await self._append_plain(text, classes="system-msg")

    async def _export_experience(self) -> None:
        """导出已提取的经验信息到 workspace/.moma/experience_export.json。"""
        from pathlib import Path
        from sqlalchemy import select
        from app.codebase.repo_analysis.models.experience_status import MrExperienceItem
        from app.codebase.repo_mgmt.services.repo_resolver import RepoResolver
        from app.infrastructure.database import get_db_session

        async with get_db_session() as db:
            repo = await RepoResolver.get_by_path(db, str(self._workspace))
            if not repo:
                await self._append_plain("workspace 未注册", classes="error-msg")
                return
            items = (await db.scalars(
                select(MrExperienceItem)
                .where(MrExperienceItem.repo_id == repo.id)
                .where(MrExperienceItem.status == "ready")
                .order_by(MrExperienceItem.committed_at.desc())
            )).all()

        if not items:
            await self._append_plain("暂无已提取的经验", classes="system-msg")
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
        await self._append_plain(
            f"已导出 {len(data)} 条经验到 {export_file}",
            classes="system-msg",
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt-input":
            return
        picker = self.query_one("#session-picker", SessionPicker)
        if picker.is_open:
            # 会话选择器打开时，输入变化不驱动斜杠菜单
            return
        self.query_one("#slash-menu", SlashCommandMenu).refresh_for_input(event.value)

    def on_key(self, event: events.Key) -> None:
        # 菜单/选择器导航已由 priority bindings（menu_up/down/escape）处理
        # 这里只保留 Tab 补全：Screen 默认 tab->focus_next 会先吃，
        # 但菜单开时希望拦截补全而非切焦点
        if event.key == "tab":
            menu = self.query_one("#slash-menu", SlashCommandMenu)
            if menu.is_open:
                cmd = menu.highlighted_command()
                if cmd is not None:
                    prompt = self.query_one("#prompt-input", Input)
                    suffix = " " if cmd.name in ("model", "resume") else ""
                    prompt.value = f"/{cmd.name}{suffix}"
                    prompt.cursor_position = len(prompt.value)
                    menu.hide()
                event.prevent_default()
                event.stop()

    def action_menu_up(self) -> None:
        """斜杠菜单 / 会话选择器：上移高亮。菜单关闭时什么都不做。"""
        picker = self.query_one("#session-picker", SessionPicker)
        if picker.is_open:
            picker.move_highlight(-1)
            return
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        if menu.is_open:
            menu.move_highlight(-1)

    def action_menu_down(self) -> None:
        """斜杠菜单 / 会话选择器：下移高亮。菜单关闭时什么都不做。"""
        picker = self.query_one("#session-picker", SessionPicker)
        if picker.is_open:
            picker.move_highlight(1)
            return
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        if menu.is_open:
            menu.move_highlight(1)

    def action_menu_escape(self) -> None:
        """Esc：菜单/选择器开则关，否则尝试中断任务。"""
        picker = self.query_one("#session-picker", SessionPicker)
        if picker.is_open:
            picker.hide()
            return
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        if menu.is_open:
            menu.hide()
            return
        if self._agent_busy:
            self.action_interrupt_run()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if not option_id:
            return
        if event.option_list.id == "session-picker":
            picker = self.query_one("#session-picker", SessionPicker)
            if picker.ignore_select:
                return
            sid = picker.session_id_at(option_id) or picker.highlighted_session_id()
            if not sid:
                return
            picker.hide()
            self.query_one("#prompt-input", Input).value = ""
            await self._resume_session(sid)
            return
        if event.option_list.id != "slash-menu":
            return
        prompt = self.query_one("#prompt-input", Input)
        menu = self.query_one("#slash-menu", SlashCommandMenu)
        menu.hide()
        if option_id == "model":
            prompt.value = "/model "
            prompt.cursor_position = len(prompt.value)
            prompt.focus()
            return
        if option_id in ("resume", "continue"):
            prompt.value = ""
            await self._open_resume_picker()
            return
        prompt.value = ""
        handled = await self._handle_command(f"/{option_id}")
        if handled is False:
            self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt-input":
            return
        t_submit = time.monotonic()
        picker = self.query_one("#session-picker", SessionPicker)
        if picker.is_open:
            sid = picker.highlighted_session_id()
            picker.hide()
            event.input.value = ""
            if sid:
                await self._resume_session(sid)
            return

        menu = self.query_one("#slash-menu", SlashCommandMenu)
        text = (event.value or "").strip()
        if menu.is_open and text.startswith("/") and " " not in text:
            cmd = menu.highlighted_command()
            if cmd is not None:
                text = f"/{cmd.name}"
                if cmd.name == "model":
                    event.input.value = "/model "
                    event.input.cursor_position = len(event.input.value)
                    menu.hide()
                    return
                if cmd.name in ("resume", "continue"):
                    menu.hide()
                    event.input.value = ""
                    await self._open_resume_picker()
                    return
        t0 = time.monotonic()
        menu.hide()
        event.input.value = ""
        t1 = time.monotonic()
        if not text:
            return
        if text.startswith("/"):
            handled = await self._handle_command(text)
            if handled is False:
                self.exit()
            return
        t2 = time.monotonic()
        await self._append_user(text)
        t3 = time.monotonic()
        if self._agent_busy:
            # A 运行中：B/C 进队列，等当前 turn 结束（正常完成或被 Ctrl+C 停止）后自动跑
            queue = self._pending_for(self._session_id)
            queue.append(text)
            await self._append_plain(
                f"(queued: {len(queue)} waiting)",
                classes="system-msg",
            )
            self._refresh_status()
            return
        # 立即开跑：输入框保持可用，用户可继续输入排队
        self._agent_busy = True
        self._ctrl_c_armed = False
        t4 = time.monotonic()
        self._start_run_status()
        t5 = time.monotonic()
        self._refresh_status()
        self._presenter = OutputPresenter()
        t6 = time.monotonic()
        logging.warning(
            "[input-timing] submit→menu_hide=%.3f clear→append=%.3f append=%.3f start_run=%.3f refresh=%.3f total=%.3f",
            t0 - t_submit, t2 - t1, t3 - t2, t5 - t4, t6 - t5, t6 - t_submit,
        )
        self.run_worker(
            self._run_turn(text),
            exclusive=False,
            group="agent-run",
            exit_on_error=False,
        )

    async def _run_turn(self, text: str) -> None:
        # 捕获 turn 开始时的 session_id：运行中用户切走时，drain 队列与 UI 状态都只针对该 session
        target_session = self._session_id
        try:
            # 延迟创建 session：用户发第一条消息时才落库，避免打开就退留下空 session
            if not target_session:
                target_session = await self._runner.create_session(
                    user_id=self._user_id or "cli",
                    workspace_path=str(self._workspace),
                    llm_provider=self._llm_provider,
                    llm_model=self._llm_model,
                )
                self._session_id = target_session
                self._refresh_status()
                await self._refresh_recent()
            await self._runner.run_turn(target_session, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("agent turn failed")
            # 仅当仍在该 session 上时才把错误打到 UI，避免污染切走后的新 session
            if target_session == self._session_id:
                await self._append_plain(f"Error: {exc}", classes="error-msg")
        finally:
            # 用户已切到别的 session：不动队列、不碰 UI；队列留给切回时处理
            if target_session != self._session_id:
                return
            # 当前 turn 结束（含被 Ctrl+C 停止）：若有排队消息，自动跑下一条
            # 注意：stop_current 只设 abort 标志，run_turn 会优雅返回，这里继续 drain 队列
            queue = self._pending_for(target_session)
            if queue:
                next_text = queue.pop(0)
                self._ctrl_c_armed = False
                self._presenter = OutputPresenter()
                self._run_phase = "Thinking"
                self._refresh_status()
                self.run_worker(
                    self._run_turn(next_text),
                    exclusive=False,
                    group="agent-run",
                    exit_on_error=False,
                )
            elif self._agent_busy:
                self._release_input()

    def _copy_content_to_clipboard(self) -> str:
        """获取当前选中文本并复制到系统剪贴板。

        统一选区：不论在输入区还是历史区，有选中就复制，没选中就不复制。
        """
        text = ""
        try:
            text = self.screen.get_selected_text()
        except Exception:
            pass
        if text:
            self.copy_to_clipboard(text)
        return text

    def action_interrupt_or_quit(self) -> None:
        """Ctrl+C 信号处理（三段判定）：

        - gap < 50ms：按键重复，忽略
        - 50ms <= gap < 200ms：人为双击 → 运行中则中断任务，空闲则退出
        - gap >= 200ms：单次按键 → 复制当前选中/缓冲区内容到剪贴板
        """
        now = time.monotonic()
        gap = now - self._last_ctrl_c_at
        self._last_ctrl_c_at = now
        if gap < 0.05:
            # 按键重复：忽略，但已更新时间戳，避免下一重复落入双击窗口
            return
        if gap < 0.2:
            # 人为双击：重置时间戳，避免三连击被误判
            self._last_ctrl_c_at = 0.0
            if self._agent_busy:
                self._ctrl_c_armed = True
                self.run_worker(
                    self._soft_stop_and_hint(),
                    exclusive=False,
                    group="agent-stop",
                    exit_on_error=False,
                )
                return
            self.exit()
            return
        # 单次按键：复制选中或缓冲区内容到剪贴板
        copied = self._copy_content_to_clipboard()
        if copied:
            self.notify("已复制到剪贴板", title="Ctrl+C")
        else:
            self.notify("无可复制内容", title="Ctrl+C")

    def action_interrupt_run(self) -> None:
        """Esc：仅中断当前任务，不退出。"""
        if not self._agent_busy:
            return
        if not self._ctrl_c_armed:
            self._ctrl_c_armed = True
        self.run_worker(
            self._soft_stop(),
            exclusive=False,
            group="agent-stop",
            exit_on_error=False,
        )

    async def _soft_stop_and_hint(self) -> None:
        msg = await self._runner.stop_current()
        await self._append_plain(msg, classes="system-msg")
        await self._append_plain(
            "Interrupted. Double-press Ctrl+C to exit.",
            classes="system-msg",
        )

    def _handle_exception(self, error: Exception) -> None:
        """记录崩溃现场后再交给 Textual（默认仍会退出），便于定位 Windows 控制台问题。"""
        try:
            import traceback
            self._crash_log.write_text(
                "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                encoding="utf-8",
            )
            logging.exception("TUI fatal exception -> %s", self._crash_log)
        except Exception:
            logging.exception("TUI fatal exception (crash log write failed)")
        super()._handle_exception(error)

    async def _soft_stop(self) -> None:
        msg = await self._runner.stop_current()
        await self._append_plain(msg, classes="system-msg")

    async def _handle_command(self, line: str) -> bool | None:
        raw = line.strip()
        parts = raw.split(maxsplit=1)
        name = parts[0][1:].casefold() if parts and parts[0].startswith("/") else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if name in ("exit", "quit", "q"):
            return False
        if name in ("help", "commands"):
            await self._append_assistant_markdown(SLASH_COMMANDS.help_markdown())
            return True
        if name == "status":
            await self._append_plain(self._runner.status_text())
            return True
        if name == "stop":
            await self._append_plain(await self._runner.stop_current())
            return True
        if name == "kill":
            await self._append_plain(await self._runner.stop_current(hard=True))
            return True
        if name == "queue":
            current_queue = self._pending_for(self._session_id)
            if arg == "clear":
                n = len(current_queue)
                current_queue.clear()
                await self._append_plain(
                    f"Cleared {n} queued message(s)." if n else "Queue is empty.",
                    classes="system-msg",
                )
                self._refresh_status()
                return True
            if not current_queue:
                await self._append_plain("Queue is empty.", classes="system-msg")
                return True
            lines = [f"Queue ({len(current_queue)} waiting):"]
            for i, msg in enumerate(current_queue, 1):
                preview = msg if len(msg) <= 60 else msg[:57] + "..."
                lines.append(f"  {i}. {preview}")
            await self._append_plain("\n".join(lines), classes="system-msg")
            return True
        if name == "new":
            # 临时 session：不立即落库，等用户发第一条消息时 _run_turn 会调 create_session。
            # 关窗口或切走时无需清理，也不会污染 /resume 列表（临时 session 从未进 backend）。
            self._cancel_live_timer()
            self._runner.bind_session(None)
            self._session_id = None
            self._presenter = OutputPresenter()
            # 临时 session 无 agent -> IDLE
            self._agent_busy = False
            self._stop_run_status()
            await self._wipe_chat_view()
            await self._refresh_recent()
            self._refresh_header()
            self._refresh_status()
            await self._append_plain(
                "New session: type a message to start.", classes="system-msg"
            )
            return True
        if name == "clear":
            await self._clear_chat()
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
        known = SLASH_COMMANDS.resolve(raw)
        if known is None:
            await self._append_plain(
                f"Unknown command: {line}\nType /help for available commands."
            )
        else:
            await self._append_plain(f"Command /{known.name} is not available yet.")
        return True

    async def _list_cli_sessions(self) -> list:
        sessions = await SESSION_MANAGER.get_all_sessions(
            channel_type="cli",
            user_id=self._user_id,
        )
        visible = [s for s in sessions if not getattr(s, "is_internal", False)]
        visible.sort(
            key=lambda s: getattr(s, "last_updated", None) or datetime.min,
            reverse=True,
        )
        return visible

    async def _open_resume_picker(self) -> None:
        if self._agent_busy:
            await self._append_plain("Stop the current task before switching sessions.")
            return
        self.query_one("#slash-menu", SlashCommandMenu).hide()
        try:
            sessions = await self._list_cli_sessions()
        except Exception as exc:
            await self._append_plain(f"Failed to list sessions: {exc}", classes="error-msg")
            return
        if not sessions:
            await self._append_plain("No CLI sessions yet.")
            return
        picker = self.query_one("#session-picker", SessionPicker)
        picker.show_sessions(sessions[:20], current_session_id=self._session_id)
        self.query_one("#prompt-input", Input).focus()

    async def _resume_session(self, session_id: str) -> None:
        t0 = time.monotonic()
        target = (session_id or "").strip()
        if not target:
            await self._append_plain("Usage: /resume [session_id]")
            return
        if target == self._session_id:
            self.query_one("#session-picker", SessionPicker).hide()
            await self._append_plain(f"Already on session: {target}")
            return
        t1 = time.monotonic()
        session = await SESSION_MANAGER.get_session(target)
        t2 = time.monotonic()
        if session is None:
            # 允许用短 id 后缀匹配
            sessions = await self._list_cli_sessions()
            matches = [
                s for s in sessions
                if s.session_id == target or s.session_id.endswith(target)
            ]
            if len(matches) == 1:
                session = matches[0]
                target = session.session_id
            elif len(matches) > 1:
                await self._append_plain(
                    f"Ambiguous session id `{target}`; use full id from /resume."
                )
                return
            else:
                await self._append_plain(f"Session not found: {target}", classes="error-msg")
                return
        t3 = time.monotonic()
        # 切走前：取消当前 session 的流式直播定时器，避免回调误写新 session 的 UI
        self._cancel_live_timer()
        self._runner.bind_session(target)
        self._session_id = target
        self._llm_provider = getattr(session, "llm_provider", "") or self._llm_provider
        self._llm_model = getattr(session, "llm_model", "") or self._llm_model
        ws = getattr(session, "workspace_path", None) or ""
        if ws:
            self._workspace = Path(ws).expanduser().resolve()
        self._presenter = OutputPresenter()
        # 按目标 session 的 agent 状态恢复 busy 标志：后台仍在跑的不打断，空闲的立即释放输入
        target_state = self._runner.agent_state(target)
        if target_state == "IDLE":
            self._agent_busy = False
            self._stop_run_status()
        else:
            if not self._agent_busy:
                self._agent_busy = True
            self._start_run_status()
        t4 = time.monotonic()
        await self._wipe_chat_view()
        t5 = time.monotonic()
        await self._render_session_preview(target)
        t6 = time.monotonic()
        await self._refresh_recent()
        self._refresh_header()
        self._refresh_status()
        t7 = time.monotonic()
        await self._append_plain(f"Resumed session: {target}", classes="system-msg")
        self.query_one("#prompt-input", Input).focus()
        t8 = time.monotonic()
        logging.warning(
            "[resume-timing] total=%.2fs | get_session=%.3f resolve=%.3f setup=%.3f wipe=%.3f render_preview=%.3f refresh=%.3f finalize=%.3f",
            t8 - t0, t2 - t1, t3 - t2, t4 - t3, t5 - t4, t6 - t5, t7 - t6, t8 - t7,
        )

    async def _render_session_preview(self, session_id: str) -> None:
        """回放最近 20 条 session 历史：加载期间禁用输入，加载完再恢复。"""
        prompt = self.query_one("#prompt-input", Input)
        prompt.disabled = True

        t0 = time.monotonic()
        messages = await SESSION_MANAGER.get_messages(session_id)
        t1 = time.monotonic()
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
        self._history_messages = visible
        t2 = time.monotonic()

        preview = visible[-20:]
        for msg in preview:
            if msg.role == Role.USER:
                await self._append_user((msg.content or "").strip())
            elif msg.role == Role.ASSISTANT:
                content = (msg.content or "").strip()
                if content:
                    # 历史预览用纯 Text，避免 Markdown 渲染卡顿
                    await self._mount_chat_widget(Static("MOMA", classes="assistant-label"))
                    await self._mount_chat_widget(Static(Text(content), classes="assistant-md"))
            elif msg.role == Role.TOOL:
                payload = msg.to_user_message().get("content", "")
                kind, formatted = format_outbound_content(payload)
                if kind == "tool" and formatted:
                    await self._append_tool_line(
                        formatted, success=msg.success, tool_call_id=msg.tool_call_id
                    )
        t3 = time.monotonic()

        prompt.disabled = False
        prompt.focus()
        logging.warning(
            "[preview-timing] total_msg=%d visible=%d render=%d | get_messages=%.3f filter=%.3f render_widgets=%.3f",
            len(messages), len(visible), len(preview),
            t1 - t0, t2 - t1, t3 - t2,
        )

    async def _handle_model_command(self, arg: str) -> None:
        if not arg:
            await self._append_plain(f"Current model: {self._format_model()}")
            await self._append_plain("Usage: /model provider/model_name")
            return
        provider, model = self._parse_model_arg(arg)
        if not model:
            await self._append_plain("Usage: /model provider/model_name")
            return
        self._llm_provider = provider
        self._llm_model = model
        # session 尚未创建（用户还没发消息）时只缓存 provider/model；
        # 延迟创建 session 时 _run_turn 会把 self._llm_provider/_llm_model 传给 create_session
        if self._session_id:
            await SESSION_MANAGER.update_session(
                self._session_id,
                llm_provider=provider,
                llm_model=model,
            )
            self._runner.drop_agent(self._session_id)
        self._refresh_header()
        await self._append_plain(f"Model set to: {self._format_model()}")

    @staticmethod
    def _parse_model_arg(raw: str) -> tuple[str, str]:
        text = (raw or "").strip()
        if not text:
            return "", ""
        if "/" not in text:
            return "", text
        provider, name = text.split("/", 1)
        return provider.strip(), name.strip()

    def action_request_quit(self) -> None:
        self.exit()


async def run_tui(
    *,
    session_id: Optional[str],
    workspace: Path,
    user_id: str,
    llm_provider: str = "",
    llm_model: str = "",
) -> None:
    """启动 TUI。session_id 可为 None--延迟到用户发第一条消息时才创建，避免打开就退留下空 session。"""
    app = MomaCoderApp(
        session_id=session_id,
        workspace=workspace,
        runner=AGENT_RUNNER,
        user_id=user_id,
        llm_provider=llm_provider,
        llm_model=llm_model,
    )
    try:
        await app.run_async()
    except Exception:
        logging.exception("TUI 异常退出；可查看 %%TEMP%%\\moma_tui_crash.log")
        raise
