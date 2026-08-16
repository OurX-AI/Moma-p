from typing import List, Tuple
import numpy as np
import asyncio
import voyageai
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS


class VoyageEmbed(BaseEmbedding):
    """Voyage AI嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: str = None, **kwargs):
        """
        初始化Voyage AI嵌入模型
        
        Args:
            api_key (str): Voyage AI API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (str): API基础URL（未使用）
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)
        
        self.client = voyageai.Client(api_key=api_key)

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        batch_size = 16
        ress = []
        token_count = 0
        
        for i in range(0, len(texts), batch_size):
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    res = await asyncio.to_thread(
                        self.client.embed,
                        texts=texts[i : i + batch_size], 
                        model=self.model_name, 
                        input_type="document"
                    )
                    ress.extend(res.embeddings)
                    token_count += res.total_tokens
                    break  # 成功，跳出重试循环
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"Voyage嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"Voyage嵌入编码最终失败: {e}")
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
                    self.client.embed,
                    texts=text, 
                    model=self.model_name, 
                    input_type="query"
                )
                return np.array(res.embeddings)[0], res.total_tokens
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Voyage查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Voyage查询编码最终失败: {e}")
                    raise e