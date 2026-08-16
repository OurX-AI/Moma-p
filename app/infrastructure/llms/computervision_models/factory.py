from typing import Dict, Type
from ..base_factory import BaseModelFactory
from .base import BaseComputerVision
from .openai_cv import OpenAICV
from .qwen_cv import QWenCV
from .zhipu_cv import ZhipuCV
from .gemini_cv import GeminiCV


class ComputerVisionModelFactory(BaseModelFactory[BaseComputerVision]):
    """计算机视觉模型工厂类"""
    
    @property
    def _models(self) -> Dict[str, Type[BaseComputerVision]]:
        from .ollama_cv import OllamaCV

        return {
            "openai": OpenAICV,
            "azure_openai": OpenAICV,
            "qwen": QWenCV,
            "zhipu": ZhipuCV,
            "ollama": OllamaCV,
            "gemini": GeminiCV,
            "siliconflow": OpenAICV,
        }

    def __init__(self):
        super().__init__("cv_models.json")


# 全局工厂实例
cv_factory = ComputerVisionModelFactory()
