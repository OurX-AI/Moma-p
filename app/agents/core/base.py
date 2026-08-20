import json
import asyncio
import logging
import re
from abc import ABC
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.infrastructure.llms.chat_models.schemes import TokenUsage
from ..contants import AGENT_CONFIG_DIR, AGENT_CONFIG_FILE, default_workspace_path, resolve_subagent_dir
from ..skills.manager import SkillsManager
from .run_abort import AbortReason, RunAbortController
from ..output import OutboundMessage, OutboundMessageType, emit_output
from ..sessions.manager import SESSION_MANAGER
from ..sessions.message import Role, Message
from ..schemes import AgentContext
from ..sessions.compaction import SessionCompaction
from app.config.settings import settings


class AgentState(str, Enum):
    """Agent state enumeration"""
    IDLE = "IDLE"  # Idle state
    RUNNING = "RUNNING"  # Running state
    WAITING = "WAITING"  # Waiting for user input
    ERROR = "ERROR"  # Error state
    FINISHED = "FINISHED"  # Finished state

class ToolChoice(str, Enum):
    """工具调用模式：none=不暴露工具，auto=由模型决定，required=必须调用工具。"""
    NONE = "none"
    AUTO = "auto"
    REQUIRED = "required"

class BaseAgent(ABC):
    """Base Agent class

    Base class for all agents, defining basic properties and methods.
    执行类，不参与 schema 序列化，仅用 __init__ 内 self 赋值。
    """

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
        # 会话与用户
        self.session_id = session_id
        self.user_id = user_id

        # 客户端信息
        self.channel_type = channel_type
        self.channel_id = channel_id

        # 基本信息
        self.agent_type = agent_type
        self.agent_description: str = ""
        self.is_subagent = bool(is_subagent)
        self.parent_agent_type = (parent_agent_type or "").strip()
        if self.is_subagent:
            if not self.parent_agent_type:
                raise ValueError("parent_agent_type is required when is_subagent=True")
            self.agent_path = resolve_subagent_dir(self.parent_agent_type, self.agent_type)
        else:
            self.agent_path = (AGENT_CONFIG_DIR / self.agent_type).resolve()

        # 会话/运行时指定模型
        self.llm_provider = str(llm_provider or "").strip()
        self.llm_model = str(llm_model or "").strip()

        # 提示词信息
        self.system_prompt = None
        self.dynamic_system_prompt = None
        self.user_prompt = user_prompt or ""
        self.next_step_prompt = next_step_prompt or ""

        # 工作空间（用户指定路径或默认沙箱）
        self.workspace_path = default_workspace_path(
            self.user_id,
            self.parent_agent_type if self.is_subagent else self.agent_type,
            workspace_path,
        )

        # 执行步数相关
        self._state = AgentState.IDLE
        self._current_step = 0
        self._max_steps: Optional[int] = None
        self._max_duplicate_steps = 2   # 默认最大重复次数，用于检验当前项agent是否挂死

        # 扩展参数
        self.params = dict(kwargs)
        
        # 执行过程信息
        self._stream_open = False
        self._last_llm: Any = None

        # 加载配置
        self.agent_config: dict = {}
        self._load_agent_config()

        # 成员变量
        self._abort_controller = RunAbortController()  # 异常控制（Agent 生命周期内复用，每轮 run 通过 clear 复位）
        self._memory_manager = None
        
        # 上下文
        self._agent_context = AgentContext(
            user_id=self.user_id,
            session_id=self.session_id,
            agent_type=self.agent_type,
            agent_description=self.agent_description,
            agent_path=str(self.agent_path),
            workspace_path=str(self.workspace_path),
            channel_type=self.channel_type,
            channel_id=self.channel_id,
            llms_list=list(self.llms_list),
            skills_manager=self.skills_manager,
            is_subagent=self.is_subagent,
            parent_agent_type=self.parent_agent_type,
            agent_config=dict(self.agent_config) if self.agent_config else {},
            params=self.params,
        )

    # ------------------------------------------------------------------
    # Reset相关操作
    # ------------------------------------------------------------------
    def reset(self):
        """重置 agent 状态到初始状态"""
        try:
            self._state = AgentState.IDLE
            self._current_step = 0
            self._abort_controller.clear()
            self._stream_open = False
            self._last_llm: Any = None
            # 取消后台记忆合并 task，避免退出时 "coroutine never awaited" 警告
            if hasattr(self, "_bg_tasks"):
                for t in list(self._bg_tasks):
                    t.cancel()
                self._bg_tasks.clear()
        except Exception as e:
            logging.error(f"Error in agent reset: {str(e)}")
            raise e

    # ------------------------------------------------------------------
    # Abort相关操作
    # ------------------------------------------------------------------
    def request_abort(self, reason: AbortReason, message: Optional[str] = None) -> None:
        """请求终止当前 run（用户中断、抢占、错误等多入口统一入口）。"""
        self._abort_controller.request_abort(reason, message)

    def is_aborted(self) -> bool:
        return self._abort_controller.is_aborted()

    def _abort_reason_label(self) -> str:
        if self._abort_controller.is_aborted():
            return self._abort_controller.reason_label()
        return "aborted"

    # ------------------------------------------------------------------
    # Load相关操作
    # ------------------------------------------------------------------
    def _load_agent_config(self) -> None:
        """加载 agent 目录下 config.json，填充 agent_config 与 description。"""
        cfg_path = self.agent_path / AGENT_CONFIG_FILE
        self.agent_config = {}
        if cfg_path.is_file():
            try:
                raw = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.agent_config = raw
            except Exception as e:
                logging.warning("Failed to load %s: %s", cfg_path, e)
        self.agent_description = (self.agent_config.get("description_en") or "").strip()

        self._load_run_limits()
        self._load_llms_config()
        self._load_skills_config()

    def _load_run_limits(self) -> None:
        """从根级 run_limit 读取运行步数等参数。"""
        run_limit = self.agent_config.get("run_limit")
        if not isinstance(run_limit, dict):
            return
        if "max_steps" in run_limit:
            self._max_steps = self._parse_max_steps(run_limit.get("max_steps"))
        if "max_duplicate_steps" in run_limit:
            self._max_duplicate_steps = run_limit["max_duplicate_steps"]

    @staticmethod
    def _parse_max_steps(raw: Any) -> Optional[int]:
        if raw is None:
            return None
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            raw = text
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _reached_max_steps(self) -> bool:
        """已达 max_steps 上限；未配置/空值时恒为 False。"""
        if self._max_steps is None or self._max_steps <= 0:
            return False
        return self._current_step >= int(self._max_steps)

    def _load_llms_config(self) -> None:
        """加载模型配置：agent_config.llm → 会话指定模型覆盖 → 系统默认。"""
        self.llms_list = self._llm_pairs_from_config(self.agent_config)
        if self.llm_provider or self.llm_model:
            pair = (self.llm_provider, self.llm_model)
            self.llms_list = [pair] + [x for x in self.llms_list if x != pair]
        if not self.llms_list:
            self.llms_list = [("", "")]

    @staticmethod
    def _llm_pairs_from_config(agent_config: Dict[str, Any] | None) -> List[Tuple[str, str]]:
        """从 config.llm.primary/fallback 提取非空 (provider, model) 列表。"""
        pairs: List[Tuple[str, str]] = []
        if not isinstance(agent_config, dict):
            return pairs
        
        llm_cfg = agent_config.get("llm")
        if not isinstance(llm_cfg, dict):
            return pairs
        for key in ("primary", "fallback"):
            item = llm_cfg.get(key)
            if not isinstance(item, dict):
                continue
            pair = (
                str(item.get("provider") or "").strip(),
                str(item.get("model") or "").strip(),
            )
            if pair != ("", "") and pair not in pairs:
                pairs.append(pair)
        return pairs

    def _load_skills_config(self) -> None:
        """从 agent_config 的 skills.permissions / skills.allow_manage 读取。"""
        skill_names: list[str] = []
        allow_manage = False
        external_dirs: list[str] = []

        data = self.agent_config
        if isinstance(data, dict):
            skills = data.get("skills")
            if isinstance(skills, dict):
                perm = skills.get("permissions")
                if isinstance(perm, dict):
                    skill_names = [
                        str(name)
                        for name, decision in perm.items()
                        if str(decision).strip().lower() == "allow"
                    ]
                allow_manage = str(skills.get("allow_manage", "no")).strip().lower() == "yes"
                raw_ext = skills.get("external_dirs")
                if isinstance(raw_ext, list):
                    external_dirs = [str(x).strip() for x in raw_ext if str(x).strip()]

        self.skills_manager = SkillsManager(
            self.agent_type,
            filter_skills=skill_names or None,
            allow_manage=allow_manage,
            external_dirs=external_dirs or None,
            workspace_path=self.workspace_path,
        )

    # ------------------------------------------------------------------
    # Run相关操作
    # ------------------------------------------------------------------
    async def run(self, question: str) -> str:
        """Run the agent
        
        Args:
            question: Input question
            
        Returns:
            str: Execution result
        """
        pass
 
    def handle_stuck_state(self):
        """Handle stuck state by adding a prompt to change strategy"""
        stuck_prompt = "\
        Observed duplicate responses. Consider new strategies and avoid repeating ineffective paths already attempted."
        self.next_step_prompt = f"{stuck_prompt}\n{self.next_step_prompt}"
        logging.warning(f"Agent detected stuck state. Added prompt: {stuck_prompt}")

    async def is_stuck(self) -> bool:
        """Check if the agent is stuck in a loop by detecting duplicate content"""
        history = await self.get_history_messages()
        if len(history) < 2:
            return False

        last_message = history[-1]
        if not last_message.content:
            return False

        # Count identical content occurrences
        duplicate_count = sum(
            1
            for msg in reversed(history[:-1])
            if msg.role == Role.ASSISTANT and msg.content == last_message.content
        )

        return duplicate_count >= self._max_duplicate_steps

    def get_state(self) -> AgentState:
        """Get current state
        
        Returns:
            AgentState: Current state
        """
        return self._state
    
    @staticmethod
    def _strip_think(text: str | None) -> str:
        """去掉回复中的 <think>...</think> 块（部分思考模型会内嵌），避免把思考过程当正文返回。"""
        if not text:
            return ""
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    # ------------------------------------------------------------------
    # 会话历史相关操作
    # ------------------------------------------------------------------
    async def get_history_messages(self) -> List[Message]:
        """Get messages from session"""
        return await SESSION_MANAGER.get_messages(self.session_id)

    async def get_history_context(self) -> List[Dict[str, Any]]:
        """Get history for context"""
        return await SESSION_MANAGER.get_context(self.session_id)

    async def push_history_message(self, message: Message):
        """Add message to session and push user"""
        # 记录会话历史
        await SESSION_MANAGER.add_message(self.session_id, message)

    async def notify_user(
        self,
        message: Optional[Message] = None,
        *,
        content: Optional[str] = None,
        outbound_type: OutboundMessageType = OutboundMessageType.RESPONSE,
    ) -> None:
        """经输出处理器通知用户；outbound_type 区分 response、流式 start/delta/end、整轮 run_end。"""
        if outbound_type == OutboundMessageType.STREAM_START:
            if self._stream_open:
                return
            self._stream_open = True
            text = ""
        elif outbound_type == OutboundMessageType.STREAM_DELTA:
            text = content if content is not None else (message.to_user_message().get("content", "") if message else "")
            if not text:
                return
            if not self._stream_open:
                self._stream_open = True
                await emit_output(OutboundMessage(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    content="",
                    outbound_type=OutboundMessageType.STREAM_START,
                ))
        elif outbound_type == OutboundMessageType.STREAM_END:
            if not self._stream_open:
                return
            self._stream_open = False
            text = ""
        elif outbound_type == OutboundMessageType.RUN_END:
            text = ""
        else:
            if message is not None:
                text = message.to_user_message().get("content", "")
            else:
                text = content or ""

        await emit_output(OutboundMessage(
            session_id=self.session_id,
            user_id=self.user_id,
            content=text,
            outbound_type=outbound_type,
        ))

    async def push_history_message_and_notify_user(self, message: Message):
        """Add message to session and push user"""
        await self.push_history_message(message)
        #if message.tool_call_id is None: # 显示工具调用结果消息不通知用户
        await self.notify_user(message)

    # ------------------------------------------------------------------
    # 上下文溢出相关操作
    # ------------------------------------------------------------------

    def _llm_for_context_overflow(self) -> Optional[Any]:
        """供 is_overflow 读取模型窗口：优先本轮已用 LLM，否则按 llms_list 首项预创建。

        首轮即按当前主模型配置取 context 窗口，不等待第一次 API 返回。
        """
        if self._last_llm is not None:
            return self._last_llm
        try:
            provider, model = "", ""
            if self.llms_list:
                provider, model = self.llms_list[0][0] or "", self.llms_list[0][1] or ""
            llm = llm_factory.create_model(provider=provider or None, model=model or None)
            self._last_llm = llm
            return llm
        except Exception as e:
            logging.warning("Preload LLM for context overflow limits failed: %s", e)
            return None

    async def handle_context_overflow(
        self,
        usage: Optional[TokenUsage] = None,
        force: bool = False,
        question: str = "",
        keep_last_n: Optional[int] = None,
    ) -> None:
        """每轮无条件 micro-prune，仍超或 force 才 autocompact。

        compact 决策：
        - force → 直接 compact（可用 keep_last_n 收紧保留窗口）
        - prune 清掉了内容 → 用本地再估（API usage 已过时）
        - prune 未清且调用方传了 usage → 用模型返回的 usage（更准）
        - 否则（调用前 / 本轮刚写入 tool 未进 API usage）→ 本地估
        """
        llm = self._llm_for_context_overflow()

        full_system_prompt = "\n\n---\n\n".join(
            p for p in (self.system_prompt, self.dynamic_system_prompt) if p
        ) if self.dynamic_system_prompt else self.system_prompt

        # 计算当前 token 用量，确定需要清理多少
        current_usage = await SESSION_MANAGER.estimate_session_usage(
            self.session_id,
            system_prompt=full_system_prompt or "",
            user_prompt=self.user_prompt or "",
            user_question=question or "",
        )

        # 计算可用上下文窗口
        usable_ctx = SessionCompaction.calculate_usable_context(llm)
        # 计算需要清理的 token 数（MicroCompact 核心逻辑）
        target_tokens = 0
        if current_usage.total_tokens > usable_ctx:
            # 需要清理的量 = 当前用量 - 可用窗口的 80%（留余量）
            target_tokens = current_usage.total_tokens - int(usable_ctx * 0.8)
        # 只在需要清理时调用 prune
        if target_tokens > 0:
            pruned = await SESSION_MANAGER.prune_session(self.session_id, target_tokens=target_tokens)
        else:
            pruned = 0
        
        keep = max(1, int(keep_last_n)) if keep_last_n is not None else max(1, settings.compaction_keep_last_n)
        if force:
            await SESSION_MANAGER.compact_session(
                self.session_id,
                keep_last_n=keep,
            )
            return

        if pruned > 0 or usage is None:
            usage_for_compact = await SESSION_MANAGER.estimate_session_usage(
                self.session_id,
                system_prompt=full_system_prompt or "",
                user_prompt=self.user_prompt or "",
                user_question=question or "",
            )
        else:
            usage_for_compact = usage

        if SessionCompaction.is_overflow(usage=usage_for_compact, llm=llm):
            await SESSION_MANAGER.compact_session(
                self.session_id,
                keep_last_n=keep,
            )

    async def handle_reactive_compact(
        self,
        attempt: int = 0,
    ) -> int:
        """API 已因上下文失败后的补救：强制 prune+compact，并按 attempt 收紧 keep。

        Returns:
            本轮使用的 keep_last_n
        """
        keep = SessionCompaction.reactive_keep_last_n(attempt)
        logging.warning(
            "Reactive compact: attempt=%d keep_last_n=%d session=%s",
            attempt,
            keep,
            getattr(self, "session_id", ""),
        )
        await self.handle_context_overflow(usage=None, force=True, keep_last_n=keep)
        return keep

    # ------------------------------------------------------------------
    # 记忆合并相关操作
    # ------------------------------------------------------------------
    async def consolidate_memory(self) -> None:
        '''Consolidate memory (fire-and-forget, tracked to avoid "coroutine never awaited" on shutdown).'''
        if not hasattr(self, "_bg_tasks"):
            self._bg_tasks: set[asyncio.Task] = set()

        def _on_done(task: asyncio.Task) -> None:
            self._bg_tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.warning("Memory consolidate_memory (background) failed: %s", e)

        task = asyncio.create_task(self._memory_manager.consolidate_memory(llm=self._last_llm))
        self._bg_tasks.add(task)
        task.add_done_callback(_on_done)