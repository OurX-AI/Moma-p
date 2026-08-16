"""会话管理器：按配置选用存储，提供创建、查询、更新、删除等接口。"""
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.infrastructure.llms.chat_models.schemes import TokenUsage
from app.config.settings import settings
from .message import Message
from .session import Session
from .store import SessionStore, LocalFileSessionStore, DatabaseSessionStore
from .compaction import SessionCompaction


class SessionManager:
    """会话管理器：按需加载 + 内存缓存，支持本地文件或数据库存储。"""

    def __init__(self) -> None:
        self._store: SessionStore = (
            LocalFileSessionStore() if settings.enable_local_session_storage
            else DatabaseSessionStore()
        )

    async def save_session(self, session: Session) -> None:
        """持久化会话"""
        if session:
            await self._store.save(session)

    async def create_session(
        self,
        user_id: str,
        agent_type: Optional[str] = None,
        channel_type: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        workspace_path: Optional[str] = None,
        is_internal: bool = False,
    ) -> str:
        """创建新会话。DB 由 Store 内部管理，不由 API 注入。"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        random_suffix = uuid.uuid4().hex[:8]
        session_id = f"session_{timestamp}_{random_suffix}"
        ws = workspace_path.strip() if isinstance(workspace_path, str) else workspace_path
        session = Session(
            session_id=session_id,
            user_id=user_id or "anonymous",
            description=description or "",
            agent_type=agent_type or "default",
            channel_type=channel_type or "",
            llm_provider=llm_provider or "",
            llm_model=llm_model or "",
            workspace_path=ws,
            metadata=metadata or {},
            is_internal=is_internal,
        )
        await self.save_session(session)

        logging.info("Created session: %s", session_id)
        return session_id

    async def update_session(self, session_id: str, **updates) -> bool:
        """更新会话；仅更新传入的字段（workspace_path 传空字符串表示清空）。"""
        session = await self.get_session(session_id)
        if not session:
            return False
        if "description" in updates:
            session.description = updates["description"] or session.description
        if "agent_type" in updates and updates["agent_type"]:
            session.agent_type = updates["agent_type"]
        if "channel_type" in updates:
            session.channel_type = updates["channel_type"] or session.channel_type
        if "llm_provider" in updates:
            session.llm_provider = updates["llm_provider"] or session.llm_provider
        if "llm_model" in updates and updates["llm_model"]:
            session.llm_model = updates["llm_model"]
        if "workspace_path" in updates:
            ws = updates["workspace_path"]
            session.workspace_path = ws.strip() if isinstance(ws, str) else ws
        if updates.get("metadata"):
            session.metadata.update(updates["metadata"])
        session.last_updated = datetime.now()
        await self.save_session(session)
        return True

    async def add_message(self, session_id: str, message: Message) -> bool:
        """添加消息到会话"""
        session = await self.get_session(session_id)
        if not session:
            return False
        try:
            session.messages.append(message)
            session.last_updated = datetime.now()
            await self.save_session(session)
            return True
        except Exception as e:
            logging.error("Error adding message to session %s: %s", session_id, e)
            return False

    async def get_messages(self, session_id: str) -> List[Message]:
        """Get messages from session"""
        session = await self.get_session(session_id)
        if not session:
            return []
        return session.messages or []

    async def get_context(self, session_id: str, max_messages: int = 500) -> List[Dict[str, Any]]:
        """获取会话上下文（未合并消息），供 LLM 使用。"""
        session = await self.get_session(session_id)
        if not session:
            return []
        return session.to_context(max_messages=max_messages)

    async def get_all_sessions(
        self,
        *,
        agent_type: Optional[str] = None,
        channel_type: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Session]:
        """获取会话列表；过滤条件传给 store，DB 层 WHERE 查询避免全量加载。"""
        return await self._store.get_all(
            agent_type=agent_type,
            channel_type=channel_type,
            user_id=user_id,
        )

    async def get_session(self, session_id: str) -> Optional[Session]:
        """获取会话。未命中缓存时从 store 按需加载并写入缓存。"""
        session = await self._store.get(session_id)
        if not session:
            logging.warning("Session not found: %s", session_id)
            return None
        return session

    async def delete_session(self, session_id: str) -> bool:
        """删除会话。先删 store，再清理缓存。"""
        ok = await self._store.delete(session_id)
        if not ok:
            logging.warning("Cannot delete: session not found: %s", session_id)
        return ok

    async def clear_history(self, session_id: str) -> bool:
        """清空会话历史"""
        session = await self.get_session(session_id)
        if not session:
            logging.warning("Cannot clear history: session not found: %s", session_id)
            return False
        try:
            session.clear()
            await self.save_session(session)
            logging.info("Cleared history for session: %s", session_id)
            return True
        except Exception as e:
            logging.error("Error clearing history for session %s: %s", session_id, e)
            return False

    async def compact_session(
        self,
        session_id: str,
        keep_last_n: int,
    ) -> bool:
        """对会话执行压缩：生成摘要并记录到 Session.compactions，不删除历史消息。

        Args:
            session_id: 会话 ID
            keep_last_n: 保留最近 n 条消息不参与压缩；None 时用 settings.compaction_keep_last_n

        Returns:
            是否成功
        """
        if not settings.compaction_auto:
            return True
        
        session = await self.get_session(session_id)
        if not session:
            logging.warning("Cannot compact: session not found: %s", session_id)
            return False
        msgs = session.messages or []
        if not msgs:
            return True
        compact_until = SessionCompaction.resolve_compact_until(msgs, keep_last_n)
        start = session.last_compacted if (session.compaction is not None and session.last_compacted > 0) else 0
        if compact_until <= start:
            return True
        to_summarize = msgs[start:compact_until]
        if not to_summarize:
            return True

        llm = llm_factory.create_model(provider=session.llm_provider, model=session.llm_model)
        try:
            previous_summary = session.compaction.content if session.compaction is not None else ""
            summary_message = await SessionCompaction.compact(
                llm=llm,
                messages=to_summarize,
                previous_summary=previous_summary,
            )
            if summary_message is None or not (summary_message.content or "").strip():
                logging.warning("Compaction produced empty or failed summary for session %s", session.session_id)
                return False
            session.compaction = summary_message
            session.last_compacted = compact_until
        except Exception as e:
            logging.error("Compaction failed for session %s: %s", session.session_id, e)
            return False
        session.last_updated = datetime.now()
        await self.save_session(session)
        logging.info("Compacted session %s (keep_last_n=%d)", session_id, keep_last_n)
        return True

    async def prune_session(self, session_id: str, target_tokens: int = 0) -> int:
        """对会话执行 prune：标记旧 tool result 输出为已清空（不删历史）。

        Args:
            session_id: 会话 ID
            target_tokens: 目标清理 token 数。0 表示不清理，直接返回。
                          > 0 表示基于 token 阈值的 MicroCompact 模式，清理直到释放足够 token。
        """
        if not settings.compaction_prune:
            return 0

        session = await self.get_session(session_id)
        if not session:
            logging.warning("Cannot prune: session not found: %s", session_id)
            return 0

        # 如果已有 compaction，仅从 last_compacted 之后扫描，避免重复处理更早历史段。
        start = session.last_compacted if (session.compaction is not None and session.last_compacted > 0) else 0
        pruned_tokens = SessionCompaction.prune(session.messages, start=start, target_tokens=target_tokens)
        if pruned_tokens <= 0:
            return 0
        session.last_updated = datetime.now()
        await self.save_session(session)
        logging.info("Pruned session %s (tokens=%d, target=%d)", session_id, pruned_tokens, target_tokens)
        return pruned_tokens

    async def estimate_session_usage(
        self,
        session_id: str,
        *,
        system_prompt: str = "",
        user_prompt: str = "",
        user_question: str = "",
    ) -> TokenUsage:
        """按会话当前进模上下文 + 传入的 prompt/question 本地估算 token 占用。"""
        history = await self.get_context(session_id)
        return SessionCompaction.estimate_usage(
            system_prompt=system_prompt or "",
            user_prompt=user_prompt or "",
            user_question=user_question or "",
            history=history,
        )


SESSION_MANAGER = SessionManager()
