import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Type
from .base import LLM
from .anthropic_llm import AnthropicModels
from .openai_llm import OpenAIModels
from .schemes import ChatResponse, TokenUsage
from ..base_factory import BaseModelFactory

# =============================================================================
# 聊天模型工厂
# =============================================================================

class LLMFactory(BaseModelFactory):
    """聊天模型工厂。``llm_factory.chat``：同时传入 provider 与 model_name 且已启用、未熔断时优先；否则按已启用列表（名称排序）；连续失败达阈值则约 10 分钟内跳过该模型。"""

    def _get_model_class(self, provider: str, model_para: Dict[str, Any] = None) -> Type[LLM]:
        api_type = (model_para or {}).get("api_type", "openai")
        if api_type == "anthropic":
            return AnthropicModels
        elif api_type == "openai":
            return OpenAIModels
        else:
            raise ValueError(f"未知的API类型: {api_type}")

    def __init__(self) -> None:
        super().__init__("chat_models.json")

llm_factory = LLMFactory()
