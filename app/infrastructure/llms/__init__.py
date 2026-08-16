"""
LLM模型模块
提供各类模型工厂和模型实现
"""
from .chat_models.base import LLM
from .computervision_models.base import BaseComputerVision
from .embedding_models.base import BaseEmbedding


# 各模型工厂实例
from .chat_models.factory import llm_factory
from .computervision_models.factory import cv_factory
from .embedding_models.factory import embedding_factory


__all__ = [
    # 基础类型
    "LLM",
    "BaseComputerVision",
    "BaseEmbedding",

    # 工厂实例
    "llm_factory",
    "cv_factory",
    "embedding_factory",
]