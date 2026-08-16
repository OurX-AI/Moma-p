"""会话压缩：当 token 接近上下文上限时，用摘要替代长历史。"""
import time
from typing import Any,Dict,List,Optional,Sequence
from .compaction_prompt import CompactionPrompt
from .message import Message,Role
from app.config.settings import settings
from app.infrastructure.llms.chat_models.schemes import TokenUsage
from app.infrastructure.llms.utils import num_tokens_from_string


class SessionCompaction:
    COMPACTION_BUFFER = 20_000

    @staticmethod
    def _looks_like_tool_output(text: str) -> bool:
        s = (text or "").lower()
        markers = ("<tool_call>", "<tool>", "<arg_key>", "<arg_value>", "</tool_call>")
        return any(m in s for m in markers)

    @staticmethod
    def estimate_usage(
        *,
        system_prompt: str = "",
        user_prompt: str = "",
        user_question: str = "",
        history: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> TokenUsage:
        """根据即将送入 LLM 的文本本地估算 input tokens。

        统计 system/user/question 与 history 中的 content（及 tool_calls 文本），
        不含工具 schema、请求包装等，结果偏保守低估，供调用前压力判断。
        """
        parts: List[str] = [system_prompt or "", user_prompt or "", user_question or ""]
        for item in history or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if content:
                parts.append(str(content))
            tool_calls = item.get("tool_calls")
            if tool_calls:
                parts.append(str(tool_calls))
        n = num_tokens_from_string(parts)
        return TokenUsage(input_tokens=n, total_tokens=n)

    @staticmethod
    def calculate_usable_context(llm: Optional[object] = None) -> int:
        """计算可用上下文窗口大小。

        Args:
            llm: LLM 实例，用于读取模型配置

        Returns:
            可用上下文 token 数
        """
        llm_context_limit = None
        llm_max_output_tokens = None
        if llm is not None:
            limits = getattr(llm, "limits", None)
            llm_context_limit = getattr(limits, "context_limit", None)
            llm_max_output_tokens = getattr(limits, "max_output_tokens", None)

        limit = llm_context_limit or settings.compaction_context_limit
        max_out = llm_max_output_tokens or 8192
        res = int(settings.compaction_reserved or 0)

        usable_ctx = limit - max_out - res
        return max(0, usable_ctx)

    @staticmethod
    def is_overflow(
        *,
        usage: TokenUsage,
        llm: Optional[object] = None,
    ) -> bool:
        """判断当前 token 数是否接近上下文上限，需要触发压缩。

        两种判断逻辑（优先级从高到低）：
        1. 优先使用模型的 max_input_tokens（如果模型明确设置了最大输入限制）
        2. 其次使用上下文窗口计算的可用空间（context_limit - max_output - reserved）

        Args:
            usage: 当前轮次的总 token 数（input + output 或 total）
            llm: 当前使用的 LLM 实例，用于读取模型配置（context_limit/max_tokens）

        Returns:
            True 表示溢出，应触发压缩
        """
        llm_max_input_tokens = None
        if llm is not None:
            limits = getattr(llm, "limits", None)
            llm_max_input_tokens = getattr(limits, "max_input_tokens", None)

        res = int(settings.compaction_reserved or 0)
        basis = usage.overflow_basis()

        # 逻辑1：优先检查模型的最大输入限制（如果模型明确设置了 max_input_tokens）
        # 例如 GPT-4o 的 max_input_tokens = 128k，直接使用这个值判断
        if llm_max_input_tokens is not None and llm_max_input_tokens > 0:
            usable_in = llm_max_input_tokens - res
            if usable_in > 0 and basis >= usable_in:
                return True

        # 逻辑2：使用上下文窗口计算可用空间（如果没有 max_input_tokens）
        # 计算公式：usable_ctx = context_limit - max_output - reserved
        usable_ctx = SessionCompaction.calculate_usable_context(llm)
        if usable_ctx <= 0:
            return True
        return basis >= usable_ctx

    @staticmethod
    def resolve_compact_until(messages: List[Message], keep_last_n: int) -> int:
        """计算摘要截止下标：保留尾部 keep_last_n，并向左对齐完整 tool 轮。

        避免保留窗口以孤立的 tool result 开头（缺少对应的 assistant tool_calls）。
        返回值 compact_until 表示 messages[:compact_until] 参与摘要。
        """
        if not messages:
            return 0
        keep = max(0, int(keep_last_n or 0))
        if keep <= 0:
            return len(messages)
        compact_until = max(0, len(messages) - keep)
        while compact_until > 0 and messages[compact_until].is_tool_result:
            compact_until -= 1
        return compact_until

    @staticmethod
    def reactive_keep_last_n(attempt: int) -> int:
        """Reactive compact 按尝试次数收紧保留窗口。

        attempt=0 用配置 keep；之后每次减半，下限 2。
        """
        base = max(1, int(settings.compaction_keep_last_n or 10))
        n = max(0, int(attempt or 0))
        if n <= 0:
            return base
        return max(2, base // (2 ** n))

    @staticmethod
    async def compact(
        *,
        llm: object,
        messages: List[Message],
        previous_summary: str = "",
    ) -> Optional[Message]:
        """生成会话摘要：将历史摘要（可选）与新增消息合并为新的摘要。"""
        if not messages:
            return None
        history = [m.to_context() for m in messages]
        prev = (previous_summary or "").strip()
        if prev:
            prev = CompactionPrompt.format_summary(prev)
        
        variant = "merge" if prev else "base"
        user_question = CompactionPrompt.build_user_prompt(
            variant=variant,
            previous_summary=prev,
        )
        response, _ = await llm.chat(
            system_prompt=CompactionPrompt.SYSTEM,
            user_prompt="",
            user_question=user_question,
            history=history,
            temperature=0.1,
        )
        if not response or not response.success or not response.content:
            return None
        content = CompactionPrompt.format_summary(response.content)
        if not content or content.lower().startswith("llm error:"):
            return None
        if SessionCompaction._looks_like_tool_output(content):
            return None
        # 存正文（已剥 analysis）；进模时再套续会话 preamble
        return Message(role=Role.USER, content=content)

    @staticmethod
    def prune(messages: List[Message], start: int = 0, target_tokens: int = 0) -> int:
        """清空过旧的工具结果正文，换成短 stub。

        仅处理 compactable 工具（默认 read/shell/grep/glob/web/write/edit 等）。
        基于 token 阈值清理：target_tokens > 0 时清理，直到释放足够 token。
        target_tokens == 0 时直接返回，不执行清理。

        已标 pruned_at 的跳过。返回本次清空的估算 token 数。
        """
        if not settings.compaction_prune:
            return 0

        # 不需要清理
        if target_tokens <= 0:
            return 0

        if not messages:
            return 0
        if start < 0:
            start = 0
        if start >= len(messages):
            return 0

        tools_raw = settings.compaction_prune_compactable_tools or ""
        compactable = {t.strip() for t in tools_raw.split(",") if t.strip()}
        if not compactable:
            return 0

        entries: List[Message] = []
        for msg in messages[start:]:
            if not msg.is_tool_result:
                continue
            if msg.name not in compactable:
                continue
            if isinstance(msg.metadata, dict) and msg.metadata.get("pruned_at"):
                continue
            entries.append(msg)

        if not entries:
            return 0

        # 基于 token 阈值清理（MicroCompact 核心逻辑）
        # 计算当前可清理工具的总 token
        total_tokens = sum(num_tokens_from_string(msg.content or "") for msg in entries)
        if total_tokens <= target_tokens:
            return 0  # 不需要清理

        # 从最旧的开始清理，直到释放足够 token
        to_clear = []
        cleared_tokens = 0
        for msg in entries:
            msg_tokens = num_tokens_from_string(msg.content or "")
            if cleared_tokens + msg_tokens > target_tokens:
                break
            to_clear.append(msg)
            cleared_tokens += msg_tokens

        # 执行清理
        now_ms = int(time.time() * 1000)
        for msg in to_clear:
            meta = msg.metadata if isinstance(msg.metadata, dict) else {}
            meta["pruned_at"] = now_ms
            if msg.content and len(msg.content) > 120:
                meta["pruned_preview"] = msg.content[:120]
            msg.metadata = meta
            name = (msg.name or "tool").strip() or "tool"
            msg.content = (
                f"[Old tool result cleared from context: {name}. "
                f"If you still need details, prefer a narrower re-query "
                f"(tighter path/pattern/limit) or read_file on known paths; "
                f"re-running the same broad call may be truncated again.]"
            )
        return cleared_tokens
