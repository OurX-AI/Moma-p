import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from .base import AgentState, BaseAgent, ToolChoice
from .run_abort import AbortReason
from ..tools.factory import ToolsFactory
from ..tools.policy import DELEGATION_TOOL_NAME, ToolPolicyResolver
from ..tools.scheduler import ToolRunNotifier, ToolScheduleSession
from ..sessions.message import Message, ToolCall, Function
from ..tools.schemes import ToolResultStatus
from app.infrastructure.llms.chat_models.schemes import (
    StreamEnd,
    StreamTextDelta,
    StreamToolCallReady,
    TokenUsage,
)
from app.infrastructure.llms.chat_models.base import ContextOverflowError
from ..context.context import ContextBuilder
from ..memorys.manager import register_memory
from ..output import OutboundMessageType
from ..schemes import RuntimeContext
from app.config.settings import settings
from app.infrastructure.llms.utils import call_with_llm_fallback
from ..tools.schemes import ToolCallItem, ToolResult
from ..contants import any_spawn_type_installed
from .subagent_task import SubAgentTaskRegistry
from .preprocess import Preprocess


_LLM_ABORT_POLL_SEC = 0.2


class ReActAgent(BaseAgent):
    """ReAct 执行类，属性仅在 __init__ 内通过 self 赋值。"""

    def __init__(
        self,
        user_id: str,
        session_id: str,
        channel_type: str,
        channel_id: str,
        agent_type: str,
        workspace_path: Optional[str] = None,
        user_prompt: Optional[str] = None,
        next_step_prompt: Optional[str] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        *,
        is_subagent: bool = False,
        parent_agent_type: Optional[str] = None,
        **kwargs: Any,
    ):
        super().__init__(
            user_id=user_id,
            session_id=session_id,
            channel_type=channel_type,
            channel_id=channel_id,
            agent_type=agent_type,
            workspace_path=workspace_path,
            user_prompt=user_prompt,
            next_step_prompt=next_step_prompt,
            llm_provider=llm_provider,
            llm_model=llm_model,
            is_subagent=is_subagent,
            parent_agent_type=parent_agent_type,
            **kwargs,
        )

        # 初始化子Agent上下文
        self._init_subagent_context()

        # 工具信息
        self.tool_choices = ToolChoice.AUTO
        self.special_tool_names: List[str] = ["ask_question"]
        self._available_tools = None
        self._mcp_bridge = None
        self._init_tools_factory()

    # ------------------------------------------------------------------
    # Reset相关操作
    # ------------------------------------------------------------------
    def reset(self):
        '''
        Reset the agent
        '''
        super().reset()
        if self.subagent_manager:
            self.subagent_manager.cancel_tasks()
        self._mcp_bridge = None

    # ------------------------------------------------------------------
    # Abort相关操作
    # ------------------------------------------------------------------
    def request_abort(self, reason: AbortReason, message: Optional[str] = None) -> None:
        super().request_abort(reason, message)
        if self.subagent_manager:
            self.subagent_manager.cancel_tasks()

    def _init_subagent_context(self) -> None:
        '''
        Initialize sub agent context
        '''
        from .subagent import SubAgentManager
        self.subagent_manager = SubAgentManager(
            user_id=self.user_id,
            session_id=self.session_id,
            channel_type=self.channel_type,
            channel_id=self.channel_id,
            parent_agent_type=self.agent_type,
            workspace_path=str(self.workspace_path),
            llm_provider=self.llm_provider,
            llm_model=self.llm_model,
            **self.params,
        )
        self._agent_context.subagent_manager = self.subagent_manager

    def _init_tools_factory(self) -> None:
        """根据 agent_config.tools（permissions / toolset / mode）解析并注册工具。"""
        try:
            if not self.agent_config:
                raise ValueError("Agent tools configuration is required")
            usable_tool_names = ToolPolicyResolver.resolve_agent_tools(self.agent_config)

            # 如果配置允许管理skill，则需要加上skill管理工具
            if self.skills_manager:
                if not self.skills_manager.allow_manage:
                    usable_tool_names = [n for n in usable_tool_names if n != "skill_manage"]
                elif "skill_manage" not in usable_tool_names:
                    usable_tool_names = sorted({*usable_tool_names, "skill_manage"})

            # 校验 spawn 配置与 subAgent 目录一致性
            if DELEGATION_TOOL_NAME in usable_tool_names:
                tools_block = self.agent_config.get("tools") if isinstance(self.agent_config.get("tools"), dict) else {}
                spawn_cfg = tools_block.get("spawn") if isinstance(tools_block.get("spawn"), dict) else {}
                allow_types = spawn_cfg.get("allow_types") if isinstance(spawn_cfg.get("allow_types"), list) else []
                if not any_spawn_type_installed(self.agent_type, allow_types):
                    usable_tool_names = [n for n in usable_tool_names if n != DELEGATION_TOOL_NAME]

            self._available_tools = ToolsFactory.from_permissions(
                allowed_names=usable_tool_names,
                ctx=self._agent_context,
            )
            # 为SubAgent绑定主Agent的工具
            self.subagent_manager.bind_parent_tools(
                self._available_tools.list_tool_names(),
                self.agent_config,
            )
        except Exception as e:
            logging.error(f"Error in agent tools registration: {str(e)}")
            raise e

    async def _connect_mcp_tools(self) -> None:
        """从 agent_config.mcp_servers 加载 MCP；默认 lazy 时仅注册元工具，完整 schema 经 mcp_search_tools activate。"""
        ms = self.agent_config.get("mcp_servers")
        servers = list(ms) if isinstance(ms, list) else []
        if not servers:
            return
        try:
            from ..mcp.connector import MCPServerConnector
            self._mcp_bridge = await MCPServerConnector.connect_and_register(
                servers,
                self._available_tools,
            )
        except Exception as e:
            logging.error("Failed to connect MCP servers (will retry next run): %s", e)

    async def _build_prompt_and_question(self, question: str, run_ctx: RuntimeContext) -> Tuple[str, str]:
        '''
        构建系统提示词、用户提示词、下一步提示词、和用户问题。
        包含用户消息预处理（判定任务类型 + 改写问题）+ 追加任务类型指引到 system prompt。

        Args:
            question: 原始用户消息
        Returns:
            (refined_question, question_with_runtime):
            - refined_question: 预处理改写后的用户问题（供 history 记录，不含 runtime context）
            - question_with_runtime: 处理后的用户问题（含 runtime context，供 LLM 使用）
        '''
        context_builder = ContextBuilder(
            ctx=self._agent_context,
            skills_manager=self.skills_manager,
            memory_manager=self._memory_manager
        )
        self.system_prompt, self.dynamic_system_prompt = await context_builder.build_system_prompt(sys_prompt_cache=self.system_prompt)

        # 用于问题预处理
        # 1. 用户问题让LLM确认修正，同时对问题分类，根据分类追加Prompt
        # 2. 图片处理 + 注入 runtime context
        history = await self.get_history_context()
        refined_question, task_prompt_section = await Preprocess.preprocess_user_question(
            question=question,
            history=history,
            agent_ctx=self._agent_context,
            run_ctx=run_ctx,
        )
        if task_prompt_section:
           self.dynamic_system_prompt = f"{self.dynamic_system_prompt}\n\n---\n\n{task_prompt_section}"
        question_with_runtime = await context_builder.build_user_content(refined_question)

        return refined_question, question_with_runtime

    async def _create_run_context(self) ->RuntimeContext:
        '''
        Create run context
        '''
        return RuntimeContext(
                last_llm=self._last_llm,
                actor_id=f"main:{self.session_id}" if self.session_id else "main:unknown",
                abort_controller=self._abort_controller,
                notify_user_callback=self.notify_user,
                mcp_bridge=self._mcp_bridge,
                params=dict(self.params),
            )

    async def _refresh_mcp_lease(self) -> None:
        """已建连的 MCP 在 run 期间 pin；lazy 首次建连后补 pin。"""
        if self._mcp_bridge:
            await self._mcp_bridge.pin_and_touch_connected()

    async def run(self, original_question: str, *, is_internal: bool = False) -> str:
        """Run the agent
        
        Args:
            original_question: Input original question
            
        Returns:
            str: Execution result
        """
        if not self.session_id:
            raise ValueError("Session ID is required")
        
        # 检查并重置状态
        if self._state != AgentState.IDLE:
            logging.warning(f"Agent is busy with state {self._state}, resetting...")
            self.reset()
        
        # 设置运行状态
        self._state = AgentState.RUNNING
        self._stream_open = False

        try:
            # 设置添加用户消息到history标志
            content = ""
            had_push_user_message = False
            reactive_overflow_attempts = 0
            subagent_wait_done = False

            # 连接并注册 MCP 工具
            self._memory_manager = register_memory(memory_type="default", ctx=self._agent_context)
            await self._connect_mcp_tools()
            run_ctx = await self._create_run_context()

            # 构建提示词 + 用户消息预处理。返回refined_question 给 history（干净版），question 给 LLM（含 runtime context）
            refined_question, question = await self._build_prompt_and_question(original_question, run_ctx)
            if refined_question != original_question:
                await self.notify_user(Message.system_message(refined_question))

            # ReAct 主循环：任一步 is_aborted() 为真则不再调度新一轮 think_and_act
            while not self._reached_max_steps() and self._state != AgentState.FINISHED and not self.is_aborted():
                self._current_step += 1
                await self._refresh_mcp_lease()
                # 中途已完成的 async 子任务写入 History，供本轮/下轮推理使用
                await self._push_finished_subagents_to_history()

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
                    await self.push_history_message(Message.system_message(refined_question) if is_internal else Message.user_message(refined_question))
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

                    # 模型要收工但仍有执行中的 async：wait 齐 → 下轮开头 push → 再合成一轮
                    if await self._need_wait_subagent(subagent_wait_done):
                        subagent_wait_done = True
                        question = SubAgentTaskRegistry.FINALIZE_PROMPT
                        continue

                    if content:
                        await self.push_history_message(Message.assistant_message(content))

                    break
                
                # 如果异终止，不需要后面上下文判断与压缩
                if self.is_aborted():
                    break

                # 检查上下文：再 prune（本轮新 tool 可能超 keepRecent），紧张则 compact。
                # 本轮若刚写入 tool result，API usage 不含这批正文 → 传 question 走本地再估
                if tool_calls:
                    await self.handle_context_overflow(question="")
                else:
                    await self.handle_context_overflow(usage)

                # 检查模型是否进行死循环
                if await self.is_stuck():
                    self.handle_stuck_state()

                question = self.next_step_prompt

            # 检查是否达到最大步数（仅 modes 配置了 max_steps 时生效）
            if self._reached_max_steps() and not self.is_aborted():
                self.request_abort(AbortReason.MAX_STEPS)

            # 统一异常处理
            if self.is_aborted():
                notice = f"Run aborted: {self._abort_reason_label()}."
                await self.push_history_message_and_notify_user(Message.assistant_message(notice))
                return notice
            return content
        except asyncio.CancelledError:
            if not self.is_aborted():
                self.request_abort(AbortReason.TASK_CANCELLED)
            if not had_push_user_message:
                await self.push_history_message(Message.system_message(original_question) if is_internal else Message.user_message(original_question))
            notice = f"Run aborted: {self._abort_reason_label()}."
            await self.push_history_message_and_notify_user(Message.assistant_message(notice))
            return notice
        except Exception as e:
            # 异常也标记 RUNTIME_ERROR，便于日志与后续 Phase 1 长工具协作退出
            self.request_abort(AbortReason.RUNTIME_ERROR, str(e))
            self._state = AgentState.ERROR
            if not had_push_user_message:
                await self.push_history_message(Message.system_message(original_question) if is_internal else Message.user_message(original_question))
            await self.push_history_message_and_notify_user(Message.assistant_message(f"Error in agent execution: {str(e)}"))
            raise
        finally:
            if self._mcp_bridge:
                await self._mcp_bridge.unpin_all()
            if self._stream_open:
                await self.notify_user(outbound_type=OutboundMessageType.STREAM_END)
            await self.notify_user(outbound_type=OutboundMessageType.RUN_END)
            # 记忆提取
            if self._memory_manager is not None:
                await self.consolidate_memory()
            self.reset()

    async def _push_finished_subagents_to_history(self) -> int:
        """将已结束且未投递的 async 子任务结果写入主 History。"""
        if self.subagent_manager is None:
            return 0
        records = self.subagent_manager.take_undelivered_async_finished()
        for record in records:
            await self.push_history_message_and_notify_user(
                Message.system_message(record.history_notice())
            )
        return len(records)

    async def _need_wait_subagent(self, subagent_wait_done: bool) -> bool:
        """收工前若仍有执行中的 async：wait 齐后返回 True，以便再进一轮合成。"""
        if subagent_wait_done or self.is_aborted():
            return False
        if self.subagent_manager is None or not self.subagent_manager.has_running_async():
            return False
        logging.info("Waiting for async subagents before run finalize")
        await self.subagent_manager.wait_async_tasks()
        if self.is_aborted():
            return False
        return True

    async def think_and_act(
        self,
        question: str,
        run_ctx: RuntimeContext,
    ) -> Tuple[str, List[ToolCall], TokenUsage, Optional[List[Tuple[ToolCall, ToolResult]]]]:
        """思考：无工具走 think_only，有工具走 think_with_act。"""
        if self.tool_choices == ToolChoice.NONE:
            content, usage = await self.think_only(question)
            return content, [], usage, None
        else:
            content, tool_calls, usage, tool_pairs = await self.think_with_act(question, run_ctx)
            return content, tool_calls, usage, tool_pairs

    async def think_only(
        self,
        question: str,
    ) -> Tuple[str, TokenUsage]:
        """无工具：仅 chat_stream，返回 (content, usage)。"""
        result, llm = await call_with_llm_fallback(
            self.llms_list,
            lambda llm: self._think_only_impl(llm, question),
        )
        self._last_llm = llm
        return result

    async def _think_only_impl(
        self,
        llm: Any,
        question: str,
    ) -> Tuple[str, TokenUsage]:
        history = await self.get_history_context()
        llm_task = asyncio.create_task(
            llm.chat_stream(
                system_prompt=self.system_prompt,
                user_prompt=self.user_prompt,
                user_question=question,
                system_prompt_dynamic=self.dynamic_system_prompt,
                history=history,
            )
        )
        try:
            while not llm_task.done():
                if self.is_aborted():
                    llm_task.cancel()
                    try:
                        await llm_task
                    except asyncio.CancelledError:
                        pass
                    return "", TokenUsage()
                await asyncio.sleep(_LLM_ABORT_POLL_SEC)
            stream, usage = llm_task.result()
        except asyncio.CancelledError:
            return "", TokenUsage()

        chunks: List[str] = []
        try:
            async for chunk in stream:
                if self.is_aborted():
                    break
                if isinstance(chunk, str) and chunk:
                    chunks.append(chunk)
                    await self.notify_user(content=chunk, outbound_type=OutboundMessageType.STREAM_DELTA)
        except asyncio.CancelledError:
            return "", TokenUsage()
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if self.is_aborted():
            return "", TokenUsage()
        return "".join(chunks), usage

    async def think_with_act(
        self,
        question: str,
        run_ctx: RuntimeContext,
    ) -> Tuple[str, List[ToolCall], TokenUsage, List[Tuple[ToolCall, ToolResult]]]:
        """有工具：流式 LLM，参数闭合即调度执行，返回 (content, tool_calls, usage, tool_pairs)。"""
        result, llm = await call_with_llm_fallback(
            self.llms_list,
            lambda llm: self._think_with_act_impl(llm, question, run_ctx),
        )
        self._last_llm = llm
        return result

    async def _think_with_act_impl(
        self,
        llm: Any,
        question: str,
        run_ctx: RuntimeContext,
    ) -> Tuple[str, List[ToolCall], TokenUsage, List[Tuple[ToolCall, ToolResult]]]:
        history = await self.get_history_context()
        llm_task = asyncio.create_task(
            llm.ask_tools_stream(
                system_prompt=self.system_prompt,
                user_prompt=self.user_prompt,
                user_question=question,
                system_prompt_dynamic=self.dynamic_system_prompt,
                history=history,
                tools=self._available_tools.to_params(),
                tool_choice=self.tool_choices.value,
            )
        )

        # 工具结果回调函数
        results_by_id: Dict[str, ToolResult] = {}
        async def on_tool_run_result(item: ToolCallItem, result: ToolResult) -> None:
            results_by_id[item.tool_call_id] = result
            await self.notify_user(
                Message.tool_result_message(
                    f"{result.result}",
                    item.tool_name,
                    item.tool_call_id,
                    result.status == ToolResultStatus.EXECUTE_SUCCESS,
                    metadata=getattr(result, "metadata", None),
                    tool_params=dict(item.tool_params or {}),
                )
            )

        # 创建工具调度器
        tool_scheduler = ToolScheduleSession(
            self._available_tools,
            ToolRunNotifier(on_tool_run_result=on_tool_run_result),
        )
        
        # 工具调度器取消回调函数
        def discard_schedule() -> None:
            if tool_scheduler is not None:
                tool_scheduler.discard_tasks()

        try:
            while not llm_task.done():
                if self.is_aborted():
                    llm_task.cancel()
                    discard_schedule()
                    try:
                        await llm_task
                    except asyncio.CancelledError:
                        pass
                    return "", [], TokenUsage(), []
                await asyncio.sleep(_LLM_ABORT_POLL_SEC)
            event_stream, usage = llm_task.result()
        except asyncio.CancelledError:
            discard_schedule()
            return "", [], TokenUsage(), []

        # 消费模型流式返回结果
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        try:
            async for event in event_stream:
                if self.is_aborted():
                    discard_schedule()
                    break
                if isinstance(event, StreamTextDelta):
                    text_parts.append(event.text)
                    await self.notify_user(content=event.text, outbound_type=OutboundMessageType.STREAM_DELTA)
                elif isinstance(event, StreamToolCallReady):
                    # 创建工具调用
                    tool_call = ToolCall(
                        id=event.id,
                        function=Function(
                            name=event.name,
                            arguments=dict(event.arguments or {}),
                        ),
                    )
                    tool_calls.append(tool_call)
                    # 提交工具调用到工具调度器
                    tool_scheduler.submit(run_ctx, ToolCallItem.from_tool_call(tool_call))
                    # 通知 UI：工具调用已发起（UI 显示 ▶ 执行中，收到结果后刷新为 ✓/✗）
                    await self.notify_user(
                        Message.tool_call_message("", tool_calls=[tool_call]),
                    )
                elif isinstance(event, StreamEnd) and isinstance(event.usage, TokenUsage):
                    usage = event.usage
        except asyncio.CancelledError:
            discard_schedule()
            return "", [], TokenUsage(), []
        finally:
            close = getattr(event_stream, "aclose", None)
            if close is not None:
                await close()

        if self.is_aborted():
            discard_schedule()
            return "", [], TokenUsage(), []

        # 如果工具调用为空，则抛出异常
        if self.tool_choices == ToolChoice.REQUIRED and not tool_calls:
            raise ValueError("Tool calls required but none provided")

        # 等待工具调度器完成
        await tool_scheduler.wait_complete(run_ctx)
        # 获取工具调用和结果
        tool_pairs: List[Tuple[ToolCall, ToolResult]] = []
        for tc in tool_calls:
            result = results_by_id.get(tc.id)
            if result is None:
                logging.warning("tool call %s missing result after schedule, skipped", tc.id)
                continue
            tool_pairs.append((tc, result))
        return "".join(text_parts), tool_calls, usage, tool_pairs

    def _has_special_tools(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        return [
            toolcall
            for toolcall in tool_calls
            if toolcall.function.name in self.special_tool_names
        ]
    
    async def _handle_special_tool(self, special_tool_calls: List[ToolCall]) -> None:
        """特殊工具本轮执行完毕后的状态处理（可扩展）。"""
        self._state = AgentState.FINISHED
        names = [tc.function.name for tc in special_tool_calls]
        logging.info("Agent finished after special tool(s): %s", ", ".join(names))

