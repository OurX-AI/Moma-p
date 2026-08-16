import logging
import os
import re
import random
from abc import ABC
from typing import List, Tuple, Optional
import numpy as np
import asyncio
from app.config.settings import Settings
from ..utils import num_tokens_from_string


# 重试配置常量
MAX_RETRY_ATTEMPTS = 3  # 最大尝试次数
RETRY_DELAY = 2  # 重试间隔（秒）
CONNECTION_TIMEOUT = 30  # 连接超时（秒）

class BaseEmbedding(ABC):
    """嵌入模型基类，定义所有嵌入模型必须实现的接口"""
    
    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: Optional[str] = None, **kwargs):
        """
        初始化嵌入模型基类
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (Optional[str]): API基础URL
            kwargs (dict): 其他参数
        """
        self.api_key = api_key
        self.model_provider = model_provider
        self.model_name = model_name
        self.base_url = base_url
        self.configs = kwargs

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        pass

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        pass

    def _total_token_count(self, respone = None, texts = None):
        """
        从响应中提取token总数
        
        Args:
            respone: API响应对象
            texts: 文本列表
            
        Returns:
            int: token总数
        """
        if respone:
            try:
                return respone.usage.total_tokens
            except Exception:
                pass

            try:
                return respone["usage"]["total_tokens"]
            except Exception:
                pass

        if texts:
            return sum([num_tokens_from_string(t) for t in texts])

        return 0

    def _get_model_cache_path(self, model_name: str) -> str:
        """
        获取模型缓存路径
        
        Args:
            model_name (str): 模型名称
            
        Returns:
            str: 模型缓存路径
        """
        settings = Settings()
        
        # 创建缓存目录
        cache_dir = os.path.join(settings.model_cache_dir, "embeddings")
        os.makedirs(cache_dir, exist_ok=True)
        
        # 清理模型名称，移除用户名前缀
        clean_model_name = re.sub(r"^[a-zA-Z0-9]+/", "", model_name)
        model_path = os.path.join(cache_dir, clean_model_name)
        
        return model_path
    
    def _is_retryable_error(self, error: Exception) -> bool:
        """判断错误是否可重试"""
        error_str = str(error).lower()
        return any(keyword in error_str for keyword in [
            'connection', 'timeout', 'network', 'temporary', 'busy', 'rate limit', 'overload', '429', '503', '502', '504', '500'
        ])
    
    def _get_delay(self, attempt: int = 0) -> float:
        """获取重试延迟时间（指数退避 + 随机抖动）"""
        # 指数退避：2^attempt * 基础延迟
        base_delay = 1.0
        exponential_delay = base_delay * (2 ** attempt)
        
        # 添加随机抖动，避免雷群效应
        jitter = random.uniform(0.5, 1.5)
        
        # 限制最大延迟为30秒
        max_delay = 30.0
        delay = min(exponential_delay * jitter, max_delay)
        
        return delay
    
    async def get_embedding_vector_size(self):
        """
        动态获取向量维度大小
        """
        try:
            vectors, _ = await self.encode(["ok"])
            return len(vectors[0])

        except Exception as e:
            logging.error(f"获取向量维度大小失败: {e}")
            raise

    async def is_strong_enough(self):
        """
        检查当前嵌入模型是否足够强大，通过压力测试验证模型能力
            
        Returns:
            bool: 模型是否足够强大
        """
        async def _is_strong_enough():
            try:
                _ = await asyncio.wait_for(
                    self.encode(["Are you strong enough!?"]), 
                    timeout=3
                )
            except asyncio.TimeoutError:
                raise Exception("Embedding model timeout")

        # Pressure test for GraphRAG task
        try:
            tasks = [_is_strong_enough() for _ in range(32)]
            await asyncio.gather(*tasks, return_exceptions=True)
            return True
        except Exception as e:
            logging.error(f"嵌入模型强度测试失败: {e}")
            return False
