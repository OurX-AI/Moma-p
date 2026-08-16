from typing import Dict, Type
from app.infrastructure.llms.base_factory import BaseModelFactory
from .baidu_yiyan_embed import BaiduYiyanEmbed
from .base import BaseEmbedding
from .bedrock_embed import BedrockEmbed
from .cohere_embed import CoHereEmbed
from .gemini_embed import GeminiEmbed
from .huggingface_embed import HuggingFaceEmbed
from .jina_embed import JinaEmbed
from .mistral_embed import MistralEmbed
from .nvidia_embed import NvidiaEmbed
from .ollama_embed import OllamaEmbed
from .openai_embed import OpenAIEmbed
from .qwen_embed import QWenEmbed
from .siliconflow_embed import SILICONFLOWEmbed
from .voyage_embed import VoyageEmbed
from .zhipu_embed import ZhipuEmbed


class EmbeddingModelFactory(BaseModelFactory[BaseEmbedding]):
    """嵌入模型工厂类"""
    
    @property
    def _models(self) -> Dict[str, Type[BaseEmbedding]]:
        return {
            "openai": OpenAIEmbed,
            "azure": OpenAIEmbed,
            "baichuan": OpenAIEmbed,
            "xinference": OpenAIEmbed,
            "qwen": QWenEmbed,
            "zhipu": ZhipuEmbed,
            "ollama": OllamaEmbed,
            "cohere": CoHereEmbed,
            "siliconflow": SILICONFLOWEmbed,
            "bedrock": BedrockEmbed,
            "gemini": GeminiEmbed,
            "nvidia": NvidiaEmbed,
            "mistral": MistralEmbed,
            "baidu_yiyan": BaiduYiyanEmbed,
            "voyage": VoyageEmbed,
            "jina": JinaEmbed,
            "huggingface": HuggingFaceEmbed,
        }

    def _get_model_class(self, provider: str, model_para=None) -> Type[BaseEmbedding]:
        if provider == "baai":
            from .baai_embed import BAAIEmbedding
            return BAAIEmbedding
        return super()._get_model_class(provider, model_para)

    def __init__(self):
        super().__init__("embedding_models.json")


# 全局工厂实例
embedding_factory = EmbeddingModelFactory()