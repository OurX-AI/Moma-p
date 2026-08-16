import logging
import asyncio
from typing import List, Tuple
import numpy as np
from zai import ZhipuAiClient
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS
from ..utils import truncate


class ZhipuEmbed(BaseEmbedding):
    """智谱AI嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str = "embedding-2", **kwargs):
        """
        初始化智谱AI嵌入模型
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为embedding-2
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, **kwargs)
        self.client = ZhipuAiClient(api_key=api_key)

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        arr = []
        token_count = 0
        MAX_LEN = -1

        if self.model_name.lower() == "embedding-2":
            MAX_LEN = 512
        if self.model_name.lower() == "embedding-3":
            MAX_LEN = 3072
        if MAX_LEN > 0:
            texts = [truncate(t, MAX_LEN) for t in texts]

        for txt in texts:
            # 重试逻辑
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    res = await asyncio.to_thread(
                        self.client.embeddings.create,
                        input=txt,
                        model=self.model_name
                    )
                    arr.append(res.data[0].embedding)
                    token_count += self._total_token_count(res)
                    break  # 成功则跳出重试循环
                    
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"智谱AI嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"智谱AI嵌入编码最终失败: {e}")
                        raise
        
        return np.array(arr), token_count

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        # 重试逻辑
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                res = await asyncio.to_thread(
                    self.client.embeddings.create,
                    input=text,
                    model=self.model_name
                )
                return np.array(res.data[0].embedding), self._total_token_count(res)
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"智谱AI查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"智谱AI查询编码最终失败: {e}")
                    raise