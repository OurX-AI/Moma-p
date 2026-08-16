import logging
import uuid
import asyncio
import time
from abc import ABC
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from app.config.settings import settings
from app.infrastructure.llms.chat_models.schemes import TokenUsage
from app.infrastructure.llms.chat_models.base import ContextOverflowError
from ..output import OutboundMessage, OutboundMessageType, emit_output
from ..schemes import RuntimeContext
from ..sessions.compaction import SessionCompaction
from ..sessions.message import Message
from ..sessions.session import Session
from ..tools.factory import ToolsFactory
from ..tools.file_state import FileStateManager, FILE_STATE_MANAGER
from ..tools.policy import ToolPolicyResolver
from ..contants import load_subagent_config
from .base import AgentState, BaseAgent
from .react import ReActAgent
from .run_abort import AbortReason
from .subagent_task import (
    SUBAGENT_DONE_MARKER,
    SubAgentTaskRecord,
    SubAgentTaskRegistry,
    SubAgentTaskStatus,
)
from ..tools.schemes import ToolResultStatus


_LLM_ABORT_POLL_SEC = 0.2


class ExploreThoroughness:
    """Explore 子 Agent 搜索深度软控制（写入 prompt，不改硬步数）。"""

    LEVELS: Tuple[str, ...] = ("quick", "medium", "very thorough")
    DEFAULT = "medium"
    EXPLORE_TYPE = "explore"
    _ALIASES = {
        "very-thorough": "very thorough",
        "very_thorough": "very thorough",
        "thorough": "very thorough",
    }

    @classmethod
    def normalize(cls, raw: str | None, *, subagent_type: str) -> Optional[str]:
        """仅 explore 生效；缺省 medium；非法值抛 ValueError。其它类型返回 None。"""
        if (subagent_type or "").strip() != cls.EXPLORE_TYPE:
            return None
        text = (raw or "").strip().lower()
        if not text:
            return cls.DEFAULT
        text = cls._ALIASES.get(text, text)
        if text not in cls.LEVELS:
            raise ValueError(
                f"thoroughness must be one of {list(cls.LEVELS)}, got {raw!r}"
            )
        return text

    @classmethod
    def runtime_block(cls, level: str) -> str:
        return (
            "## Thoroughness\n"
            f"Caller requested: **{level}**\n"
            "- quick: targeted lookup; few tool calls; stop once the answer is found\n"
            "- medium: balanced exploration across likely locations and naming variants\n"
            "- very thorough: comprehensive search across directories, naming conventions, and cross-checks before concluding\n"
            "Obey the requested level; do not over-search on quick or under-search on very thorough."
        )

    @classmethod
    def prepend_task(cls, task: str, level: str) -> str:
        body = (task or "").strip()
        prefix = f"Thoroughness: {level}"
        return f"{prefix}\n\n{body}" if body else prefix


class SubAgentManager(ABC):
    """SubAgent 管理器"""

    def __init__(
        self,
        user_id: str,
        session_id: str,
        channel_type: str,
        channel_id: str,
        parent_agent_type: str,
        workspace_path: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        **kwargs: Any,
    ):
        # 基本信息
        self.user_id = user_id
        self.parent_agent_type = parent_agent_type
        self.session_id = session_id
        self.channel_type = channel_type
        self.channel_id = channel_id
        self.workspace_path = workspace_path

        # 模型信息
        self.llm_provider = llm_provider or ""
        self.llm_model = llm_model or ""

        self.params = kwargs

        # 运行任务信息
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._task_registry = SubAgentTaskRegistry()
        self._parent_tool_names: List[str] = []
        self._parent_agent_config: Dict[str, Any] = {}

    def bind_parent_tools(self, tool_names: List[str], agent_config: Dict[str, Any]) -> None:
        """主 Agent 注册工具后同步当前可用工具名与配置（供 spawn 求交）。"""
        self._parent_tool_names = list(tool_names or [])
        self._parent_agent_config = dict(agent_config) if agent_config else {}

    def get_task_record(self, task_id: str) -> Optional[SubAgentTaskRecord]:
        return self._task_registry.get(task_id)

    def list_task_records(
        self,
        *,
        status: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[SubAgentTaskRecord]:
        return self._task_registry.list(status=status, mode=mode)

    def cancel_tasks(self) -> None:
        """父 Agent 中止时取消所有 async 子任务。"""
        for task in list(self._running_tasks.values()):
            if not task.done():
                task.cancel()
        self._task_registry.mark_cancelled_running()

    def has_running_async(self) -> bool:
        return self._task_registry.has_running(mode="async")

    def take_undelivered_async_finished(self) -> List[SubAgentTaskRecord]:
        return self._task_registry.take_undelivered_finished(mode="async")

    async def wait_async_tasks(self) -> None:
        """等待本轮仍在跑的 async 子任务结束（registry 由 done 回调同步更新）。"""
        pending = [t for t in list(self._running_tasks.values()) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)

    async def start_task(
        self,
        task: str,
        run_ctx: RuntimeContext,
        *,
        mode: str = "sync",
        label: str | None = None,
        subagent_type: str | None = None,
        thoroughness: str | None = None,
    ) -> str:
        """
        启动子任务。sync 阻塞等待；async 后台执行，由主 Agent Loop drain / 收工前 wait。
        """
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        # 作业模式
        normalized_mode = (mode or "sync").strip().lower()
        if normalized_mode not in {"sync", "async"}:
            raise ValueError("spawn mode must be 'sync' or 'async'")

        resolved_type = self._resolve_subagent_type(subagent_type)
        resolved_thoroughness = ExploreThoroughness.normalize(
            thoroughness,
            subagent_type=resolved_type,
        )
        self._task_registry.register(
            task_id=task_id,
            label=display_label,
            subagent_type=resolved_type,
            mode=normalized_mode,
            task=task,
        )
        # 同步 SubAgent 执行模式
        if normalized_mode == "sync":
            result = await self._run_subagent_task(
                task_id=task_id,
                task=task,
                label=display_label,
                run_ctx=run_ctx,
                subagent_type=resolved_type,
                thoroughness=resolved_thoroughness,
            )
            formatted = self._format_subagent_result(task_id=task_id, label=display_label, result=result)
            # sync 结果已在 tool_result 中，无需再 drain 入 History
            self._finish_registry_from_result(task_id, result, formatted, delivered=True)
            return formatted
        else:
            # 异步SubAgent执行模式
            bg_task = asyncio.create_task(
                self._run_subagent_task(
                    task_id=task_id,
                    task=task,
                    label=display_label,
                    run_ctx=run_ctx,
                    subagent_type=resolved_type,
                    thoroughness=resolved_thoroughness,
                )
            )
            self._running_tasks[task_id] = bg_task

            def _on_done(fut: asyncio.Task, *, _tid: str = task_id, _label: str = display_label) -> None:
                self._running_tasks.pop(_tid, None)
                self._settle_async_task(_tid, _label, fut)

            bg_task.add_done_callback(_on_done)
            logging.info("Started async subagent [%s] type=%s: %s", task_id, resolved_type, display_label)
            return (
                f"Subagent [{display_label}] ({resolved_type}) started asynchronously "
                f"(id: {task_id}). The main agent will pick up the result in-loop when ready."
            )

    def _resolve_subagent_type(self, requested_type: str | None) -> str:
        """解析并校验子 Agent 类型（allow_types / default_type / 目录配置）。"""
        tools_block = self._parent_agent_config.get("tools") if isinstance(self._parent_agent_config.get("tools"), dict) else {}
        spawn_cfg = tools_block.get("spawn") if isinstance(tools_block.get("spawn"), dict) else {}
        default_type = str(spawn_cfg.get("default_type") or "").strip()
        chosen = (requested_type or "").strip() or default_type
        if not chosen:
            raise ValueError("spawn type is required (pass type or set tools.spawn.default_type)")
        # 校验 allow_types / 目录配置；失败抛 ValueError
        load_subagent_config(
            self.parent_agent_type,
            chosen,
            parent_agent_config=self._parent_agent_config,
        )
        return chosen

    async def _run_subagent_task(
        self,
        task_id: str,
        task: str,
        label: str,
        run_ctx: RuntimeContext,
        subagent_type: str,
        thoroughness: str | None = None,
    ) -> Dict[str, Any]:
        # 获取父 Agent 读记录中的规范化路径列表
        parent_actor = f"main:{self.session_id}" if self.session_id else "main:unknown"
        watch_paths = FILE_STATE_MANAGER.get_read_record_paths(parent_actor)
        started_at = time.time()

        # 创建并执行子 Agent；构造/运行异常均转为失败结果，不向上冒泡打断主 Agent
        try:
            subagent = SubAgent(
                user_id=self.user_id,
                session_id=self.session_id,
                channel_type=self.channel_type,
                channel_id=self.channel_id,
                parent_agent_type=self.parent_agent_type,
                subagent_type=subagent_type,
                parent_tool_names=self._parent_tool_names,
                parent_agent_config=self._parent_agent_config,
                parent_run_ctx=run_ctx,
                task_id=task_id,
                task=task,
                label=label,
                thoroughness=thoroughness,
                workspace_path=self.workspace_path,
                llm_provider=self.llm_provider,
                llm_model=self.llm_model,
                **self.params,
            )
            result = await subagent.run()
        except Exception as e:
            logging.exception("Subagent [%s] failed: %s", task_id, e)
            return {
                "task_id": task_id,
                "task": task,
                "status": False,
                "result": f"Subagent error: {e}",
            }

        # 获取子 Agent 写记录中的规范化路径列表，提醒父 Agent 文件内容被子 Agent 修改过
        sibling_writes = FILE_STATE_MANAGER.get_write_records_since(parent_actor, started_at, watch_paths)
        reminder = FileStateManager.format_writes_since_reminder(sibling_writes)
        if reminder:
            body = (result.get("result") or "").strip()
            result["result"] = f"{body}\n\n{reminder}" if body else reminder

        return result

    def _settle_async_task(self, task_id: str, label: str, fut: asyncio.Task) -> None:
        """async 任务结束：同步更新 registry；TUI 卡片异步发出。不走内部消息队列。"""
        try:
            result = fut.result()
            content = self._format_subagent_result(task_id=task_id, label=label, result=result)
            record = self._finish_registry_from_result(task_id, result, content, delivered=False)
        except asyncio.CancelledError:
            logging.info("Async subagent [%s] cancelled", task_id)
            record = self._task_registry.mark_finished(
                task_id,
                status=SubAgentTaskStatus.CANCELLED,
                result="Subagent cancelled.",
                delivered=False,
            )
        except Exception as e:
            content = self._format_subagent_result(
                task_id=task_id,
                label=label,
                result={"task_id": task_id, "task": "", "status": False, "result": f"Error: {e}"},
            )
            record = self._task_registry.mark_finished(
                task_id,
                status=SubAgentTaskStatus.FAILED,
                result=content,
                delivered=False,
            )
        if record is None:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit_subagent_card(record))
        except RuntimeError:
            pass

    def _finish_registry_from_result(
        self,
        task_id: str,
        result: Dict[str, Any],
        formatted: str,
        *,
        delivered: bool = False,
    ) -> Optional[SubAgentTaskRecord]:
        ok = bool(result.get("status"))
        status = SubAgentTaskStatus.COMPLETED if ok else SubAgentTaskStatus.FAILED
        return self._task_registry.mark_finished(
            task_id,
            status=status,
            result=formatted,
            delivered=delivered,
        )

    async def _emit_subagent_card(self, record: SubAgentTaskRecord) -> None:
        """仅 UI 提示，不驱动主 Agent 新一轮 Run。"""
        card = SubAgentTaskRegistry.format_card_text(record)
        try:
            await emit_output(
                OutboundMessage(
                    session_id=self.session_id or "",
                    user_id=self.user_id,
                    content=card,
                    outbound_type=OutboundMessageType.SUBAGENT_DONE,
                    metadata={
                        "task_id": record.task_id,
                        "label": record.label,
                        "type": record.subagent_type,
                        "status": record.status.value,
                        "marker": SUBAGENT_DONE_MARKER,
                    },
                )
            )
        except Exception as exc:
            logging.warning("emit SUBAGENT_DONE failed task_id=%s: %s", record.task_id, exc)

    @staticmethod
    def _format_subagent_result(task_id: str, label: str, result: Dict[str, Any]) -> str:
        status = "completed successfully" if result.get("status") else "failed"
        # 回传主 Agent 时去掉思考块，只保留可执行结论
        body = BaseAgent._strip_think(result.get("result") or "") or "(empty result)"
        return (
            f"[Subagent '{label}' {status}] (id: {task_id})\n\n"
            f"Task: {result.get('task') or ''}\n\n"
            f"Result:\n{body}"
        )


class SubAgent(ReActAgent):
    """SubAgent 执行类，属性仅在 __init__ 内通过 self 赋值。"""

    def __init__(
        self,
        user_id: str,
        session_id: str,
        channel_type: str,
        channel_id: str,
        parent_agent_type: str,
        subagent_type: str,
        parent_tool_names: List[str],
        parent_agent_config: Dict[str, Any],
        parent_run_ctx: RuntimeContext,
        task_id: str,
        task: str,
        label: str,
        thoroughness: Optional[str] = None,
        workspace_path: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        **kwargs: Any,
    ):
        # 父 Agent 信息
        # super().__init__ 内会调用本类重载的 _init_tools_factory / _load_llms_config
        self.parent_agent_type = parent_agent_type
        self.parent_run_ctx = parent_run_ctx
        self._parent_tool_names = list(parent_tool_names or [])
        self._parent_agent_config = dict(parent_agent_config) if parent_agent_config else {}

        super().__init__(
            user_id=user_id,
            session_id=session_id,
            channel_type=channel_type,
            channel_id=channel_id,
            agent_type=subagent_type,
            workspace_path=workspace_path,
            llm_provider=llm_provider,
            llm_model=llm_model,
            is_subagent=True,
            parent_agent_type=parent_agent_type,
            **kwargs,
        )

        # 任务信息
        self.task_id = task_id
        self.task = task
        self.label = label        
        # 仅 explore 子 Agent 有效：搜索深度 soft 提示（quick / medium / very thorough），写入 prompt，不改硬步数上限
        self.thoroughness = thoroughness

        # 历史消息（SubAgent 独立维护，不写入主会话）
        self.history_messages: List[Message] = []
        self.compaction: Optional[Message] = None
        self.last_compacted: int = 0

    # 重载父类方法
    def _load_llms_config(self) -> None:
        """子未配置 llm 时继承父 agent_config.llm，再叠加会话指定模型。"""
        self.llms_list = self._llm_pairs_from_config(self.agent_config)
        if not self.llms_list:
            self.llms_list = self._llm_pairs_from_config(self._parent_agent_config)
        if self.llm_provider or self.llm_model:
            pair = (self.llm_provider, self.llm_model)
            self.llms_list = [pair] + [x for x in self.llms_list if x != pair]
        if not self.llms_list:
            self.llms_list = [("", "")]

    # 重载父类方法（SubAgent 一次性实例，禁止 reset 清空 abort 状态）
    def reset(self):
        pass

    # ------------------------------------------------------------------
    # Abort相关操作
    # ------------------------------------------------------------------
    def is_aborted(self) -> bool:
        """本地中止或父 Agent 中止均视为应停止（sync spawn 随主 Agent 协作退出）。"""
        if self._abort_controller.is_aborted():
            return True
        
        # 获取父中断状态，传播主Agent中断状态
        parent = self.parent_run_ctx
        return parent is not None and parent.is_aborted()

    def _abort_reason_label(self) -> str:
        if self._abort_controller.is_aborted():
            return self._abort_controller.reason_label()
        
        # 获取父中断状态，传播主Agent中断原因
        parent = self.parent_run_ctx
        ctrl = parent.abort_controller if parent else None
        if ctrl is not None and ctrl.is_aborted():
            return ctrl.reason_label()
        return "aborted"

    # 重载父类方法
    def _init_subagent_context(self) -> None:
        '''
        Initialize subagent context
        '''
        self.subagent_manager = None
        self._agent_context.subagent_manager = None

    # 重载父类方法
    def _init_tools_factory(self) -> None:
        try:
            allowed_tool_names = ToolPolicyResolver.resolve_spawn_tools(
                parent_tool_names=self._parent_tool_names,
                subagent_config=self.agent_config,
            )
            if not allowed_tool_names:
                raise ValueError("subagent allowed_tool_names is empty")
            self._available_tools = ToolsFactory.from_permissions(
                allowed_names=allowed_tool_names,
                ctx=self._agent_context,
            )
        except Exception as e:
            logging.error(f"Error in subagent tools registration: {str(e)}")
            raise e

    # 重载父类方法
    async def _build_prompt_and_question(self, question: str) -> str:
        """子 Agent system prompt：优先读 prompts/AGENT.md，再拼运行时工具与 workspace。"""
        from ..contants import AGENT_CONTEXT_PATH

        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        tool_names = ", ".join(sorted(self._available_tools.list_tool_names())) if self._available_tools else "(none)"
        profile = ""
        prompt_file = self.agent_path / AGENT_CONTEXT_PATH / "AGENT.md"
        if prompt_file.is_file():
            try:
                profile = prompt_file.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logging.warning("failed to read subagent prompt %s: %s", prompt_file, exc)
        if not profile:
            profile = (
                "You are a subagent spawned by the main agent to complete a specific task.\n"
                "Stay focused, return a concise evidence-based summary, and do not spawn other subagents."
            )
        thoroughness_block = ""
        if self.thoroughness:
            thoroughness_block = f"\n{ExploreThoroughness.runtime_block(self.thoroughness)}\n"
        self.system_prompt = f"""{profile}

## Hard Limits
- Do not send messages directly to users
- Do not spawn other subagents
- Do not use tools not listed above

When done, return a structured summary aligned with the Output Contract (or Completed / Partially Completed / Blocked if unspecified)."""
        # SubAgent 的 prompt 全部为静态内容（无 memory 动态段），同步更新 _static_system_prompt
        self.dynamic_system_prompt = f"""
## Runtime Context
- subagent_type: {self.agent_type}
- current_time: {now} ({tz})
- workspace: {self.workspace_path}
{thoroughness_block}
## Available Tools
{tool_names}
        """
        if self.thoroughness:
            return ExploreThoroughness.prepend_task(question, self.thoroughness)
        return question

    # 重载父类方法
    async def _create_run_context(self) -> RuntimeContext:
        '''
        Create run context
        '''
        return RuntimeContext(
            actor_id=f"sub:{self.task_id}" if self.task_id else "sub:unknown",
            abort_controller=self.parent_run_ctx.abort_controller,   # 使用父Agent的中断控制器
            repo_id=self.parent_run_ctx.repo_id,
            params=dict(self.parent_run_ctx.params),
            notify_user_callback=self.parent_run_ctx.notify_user_callback,
            mcp_bridge=self._mcp_bridge,
        )

    async def run(self) -> Dict[str, Any]:
        """Run the agent

        Returns:
            result: Dict[str, Any]
        """
        # SubAgent 为一次性实例（spawn 时创建、run 结束即丢弃）
        # 设置运行状态
        self._state = AgentState.RUNNING
        self._stream_open = False
        content = ""
        try:
            # 设置添加用户消息到 history 标志
            had_push_user_message = False
            reactive_overflow_attempts = 0

            await self._connect_mcp_tools()
            run_ctx = await self._create_run_context()

            # 构建提示词
            original_question = self.task
            question = await self._build_prompt_and_question(original_question)

            # ReAct 主循环：任一步 is_aborted() 为真则不再调度新一轮 think_and_act
            while not self._reached_max_steps() and self._state != AgentState.FINISHED and not self.is_aborted():
                self._current_step += 1

                # 调用前：无条件 prune，紧张时再 compact
                await self.handle_context_overflow(question=question)

                try:
                    content, tool_calls, usage, tool_pairs = await self.think_and_act(question, run_ctx)
                except ContextOverflowError:
                    # Reactive：上下文溢出则强制加压并同轮重试，连续失败则收紧 keep
                    max_reactive = max(1, int(settings.compaction_reactive_max_attempts or 3))
                    if reactive_overflow_attempts < max_reactive:
                        await self.handle_reactive_compact(attempt=reactive_overflow_attempts)
                        reactive_overflow_attempts += 1
                        continue
                    raise RuntimeError(
                        f"llm error: context_overflow "
                        f"(reactive compact exhausted after {max_reactive} attempts)"
                    )

                # 如果用户消息未推送，则推送
                if not had_push_user_message:
                    await self.push_history_message(Message.user_message(original_question))
                    had_push_user_message = True
                # 如果工具调用，则推送工具调用消息
                if tool_calls:
                    reactive_overflow_attempts = 0
                    await self.push_history_message(Message.tool_call_message(content, tool_calls=tool_calls))
                    for toolcall, tool_result in tool_pairs or []:
                        await self.push_history_message(
                            Message.tool_result_message(
                                f"{tool_result.result}",
                                toolcall.function.name,
                                toolcall.id,
                                tool_result.status == ToolResultStatus.EXECUTE_SUCCESS,
                                metadata=getattr(tool_result, "metadata", None),
                                tool_params=dict(toolcall.function.arguments or {}),
                            )
                        )
                    special_tool_calls = self._has_special_tools(tool_calls)
                    if special_tool_calls:
                        await self._handle_special_tool(special_tool_calls)
                else:
                    reactive_overflow_attempts = 0
                    if content:
                        await self.push_history_message(Message.assistant_message(content))
                        break

                # 如果已终止，不需要后面上下文判断与压缩
                if self.is_aborted():
                    break

                # 检查上下文：再 prune，紧张则 compact。
                # 本轮若刚写入 tool result，API usage 不含这批正文 → 传 question 走本地再估
                if tool_calls:
                    await self.handle_context_overflow(question="")
                else:
                    await self.handle_context_overflow(usage)

                # 检查模型是否进行死循环
                if await self.is_stuck():
                    self.handle_stuck_state()

                # 继续下一步
                question = self.next_step_prompt

            # 检查是否达到最大步数（仅 modes 配置了 max_steps 时生效）
            if self._reached_max_steps() and not self.is_aborted():
                self.request_abort(AbortReason.MAX_STEPS)

            # 统一异常处理
            if self.is_aborted():
                notice = f"Subagent aborted: {self._abort_reason_label()}."
                content = f"{content}\n\n{notice}" if content else notice

            return {
                "task_id": self.task_id,
                "label": self.label,
                "task": self.task,
                "status": not self.is_aborted(),
                "result": self._strip_think(content),
            }
        except asyncio.CancelledError:
            return {
                "task_id": self.task_id,
                "label": self.label,
                "task": self.task,
                "status": False,
                "result": f"Subagent cancelled: {self._abort_reason_label()}.",
            }
        except Exception as e:
            self._state = AgentState.ERROR
            err = f"Error in agent execution: {str(e)}"
            return {
                "task_id": self.task_id,
                "label": self.label,
                "task": self.task,
                "status": False,
                "result": f"Subagent error: {err}.",
            }

    # 重载父类方法
    async def get_history_messages(self) -> List[Message]:
        """Get messages from local history"""
        return self.history_messages

    # 重载父类方法
    async def get_history_context(self) -> List[Dict[str, Any]]:
        return Session(
            session_id=self.session_id,
            agent_type=self.agent_type,
            user_id=self.user_id,
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
            messages=self.history_messages,
            compaction=self.compaction,
            last_compacted=self.last_compacted,
        ).to_context()

    # 重载父类方法
    async def push_history_message(self, message: Message):
        """Add message to local history"""
        self.history_messages.append(message)

    # 重载父类方法
    async def notify_user(
        self,
        message: Optional[Message] = None,
        *,
        content: Optional[str] = None,
        outbound_type: OutboundMessageType = OutboundMessageType.RESPONSE,
    ) -> None:
        # SubAgent 不直接向用户推送消息
        pass

    async def handle_context_overflow(
        self,
        usage: Optional[TokenUsage] = None,
        force: bool = False,
        question: str = "",
        keep_last_n: Optional[int] = None,
    ) -> None:
        """每轮无条件 micro-prune，仍超或 force 才 autocompact（本地 history）。

        prune 清掉内容或未传 usage 时本地再估；否则复用模型返回的 usage。
        force 时可传 keep_last_n 收紧保留窗口（reactive）。
        """
        llm = self._llm_for_context_overflow()
        full_system_prompt = "\n\n---\n\n".join(
            p for p in (self.system_prompt, self.dynamic_system_prompt) if p
        ) if self.dynamic_system_prompt else self.system_prompt

        pruned = await self._prune_history()
        keep = max(1, int(keep_last_n)) if keep_last_n is not None else max(1, settings.compaction_keep_last_n)
        if force:
            await self._compact_history(keep_last_n=keep)
            return

        if pruned > 0 or usage is None:
            history = await self.get_history_context()
            usage_for_compact = SessionCompaction.estimate_usage(
                system_prompt=full_system_prompt or "",
                user_prompt=self.user_prompt or "",
                user_question=question or "",
                history=history,
            )
        else:
            usage_for_compact = usage

        if SessionCompaction.is_overflow(usage=usage_for_compact, llm=llm):
            await self._compact_history(keep_last_n=keep)

    async def _compact_history(self, keep_last_n: int = 0) -> bool:
        if not settings.compaction_auto:
            return True
        llm = self._llm_for_context_overflow()
        if not self.history_messages or llm is None:
            return True
        compact_until = SessionCompaction.resolve_compact_until(self.history_messages, keep_last_n)
        start = self.last_compacted if (self.compaction is not None and self.last_compacted > 0) else 0
        if compact_until <= start:
            return True
        to_summarize = self.history_messages[start:compact_until]
        if not to_summarize:
            return True
        previous_summary = self.compaction.content if self.compaction is not None else ""
        summary_message = await SessionCompaction.compact(
            llm=llm,
            messages=to_summarize,
            previous_summary=previous_summary,
        )
        if summary_message is None or not (summary_message.content or "").strip():
            return False
        self.compaction = summary_message
        self.last_compacted = compact_until
        return True

    async def _prune_history(self) -> int:
        """清空本地 history 中过旧的 tool result，释放上下文。

        若已有 compaction，仅从 last_compacted 之后扫描，避免重复处理已摘要段。
        返回被 prune 的估算 token 数（未触发则为 0）。
        """
        start = self.last_compacted if (self.compaction is not None and self.last_compacted > 0) else 0
        scan = self.history_messages[start:]
        return SessionCompaction.prune(scan)

    # 重载父类方法
    async def consolidate_memory(self) -> None:
        '''
        Consolidate memory
        '''
        # SubAgent 不写入长期记忆
        pass
