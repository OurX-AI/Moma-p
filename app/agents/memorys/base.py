from abc import ABC, abstractmethod
from typing import Any
from ..schemes import AgentContext


class BaseMemoryManager(ABC):
    """记忆后端抽象：与 Session 协作的合并与上下文读取接口。"""

    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx

    @property
    def init_context(self) -> AgentContext:
        return self._ctx

    @abstractmethod
    async def consolidate_memory(
        self,
        llm: Any, 
        *,
        archive_all: bool = False,
        memory_window: int = 20,
    ) -> bool:
        """将会话中待处理消息合并到持久记忆，并更新会话 last_consolidated。"""

    @abstractmethod
    async def get_memory_context(self) -> str:
        """组合各层记忆为一段 Markdown 上下文。"""
