import json
from typing import List, Tuple
import numpy as np
import asyncio
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS


class BaiduYiyanEmbed(BaseEmbedding):
    """百度文心嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: str = None, **kwargs):
        """
        初始化百度文心嵌入模型
        
        Args:
            api_key (str): 百度API密钥JSON字符串，包含yiyan_ak和yiyan_sk
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (str): API基础URL（未使用）
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)

        # 延迟导入 qianfan，避免模块加载时的依赖问题
        import qianfan

        # 解析API密钥
        try:
            key_data = json.loads(api_key)
            ak = key_data.get("yiyan_ak", "")
            sk = key_data.get("yiyan_sk", "")
        except (json.JSONDecodeError, TypeError):
            raise ValueError("API key must be a valid JSON string containing 'yiyan_ak' and 'yiyan_sk'")
        
        self.client = qianfan.Embedding(ak=ak, sk=sk)

    async def encode(self, texts: List[str], batch_size: int = 16) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            batch_size (int): 批量大小，默认为16
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                res = await asyncio.to_thread(
                    lambda: self.client.do(model=self.model_name, texts=texts).body
                )
                return (
                    np.array([r["embedding"] for r in res["data"]]),
                    self._total_token_count(res),
                )
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"百度文心嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"百度文心嵌入编码最终失败: {e}")
                    raise e
    

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                res = await asyncio.to_thread(
                    lambda: self.client.do(model=self.model_name, texts=[text]).body
                )
                return (
                    np.array([r["embedding"] for r in res["data"]]),
                    self._total_token_count(res),
                )
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"百度文心查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"百度文心查询编码最终失败: {e}")
                    raise e