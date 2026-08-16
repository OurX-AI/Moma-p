from typing import List, Tuple
import numpy as np
import asyncio
import logging
from mistralai.client import MistralClient
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS
from ..utils import truncate


class MistralEmbed(BaseEmbedding):
    """Mistral嵌入模型实现"""


    def __init__(self, api_key: str, model_provider: str, model_name: str = "mistral-embed", base_url: str = None, **kwargs):
        """
        初始化Mistral嵌入模型
        
        Args:
            api_key (str): Mistral API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为mistral-embed
            base_url (str): API基础URL（未使用，Mistral使用固定端点）
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)

        self.client = MistralClient(api_key=api_key)
    

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        texts = [truncate(t, 8196) for t in texts]
        batch_size = 16
        ress = []
        token_count = 0
        
        for i in range(0, len(texts), batch_size):
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    res = await asyncio.to_thread(
                        self.client.embeddings,
                        input=texts[i : i + batch_size], 
                        model=self.model_name
                    )
                    ress.extend([d.embedding for d in res.data])

                    token_count += self._total_token_count(res)
                    break  # 成功，跳出重试循环
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"Mistral嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"Mistral嵌入编码最终失败: {e}")
                        raise e
        
        return np.array(ress), token_count

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
                    self.client.embeddings,
                    input=[truncate(text, 8196)], 
                    model=self.model_name
                )
                
                return np.array(res.data[0].embedding), self._total_token_count(res)
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Mistral查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Mistral查询编码最终失败: {e}")
                    raise e