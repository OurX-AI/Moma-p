import time
import numpy as np
from typing import List, Tuple
import asyncio
import dashscope
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS
from ..utils import truncate


class QWenEmbed(BaseEmbedding):
    """通义千问嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str = "text_embedding_v2", **kwargs):
        """
        初始化通义千问嵌入模型
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为text_embedding_v2
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, **kwargs)

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        batch_size = 4
        res = []
        token_count = 0
        texts = [truncate(t, 2048) for t in texts]
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i : i + batch_size]
            
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    resp = await asyncio.to_thread(
                        dashscope.TextEmbedding.call,
                        model=self.model_name, 
                        input=batch_texts, 
                        api_key=self.api_key, 
                        text_type="document"
                    )
                    
                    # 检查响应是否有效
                    if resp["output"] is not None and resp["output"].get("embeddings") is not None:
                        break  # 成功，跳出重试循环
                        
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"Qwen嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"Qwen嵌入编码最终失败: {e}")
                        raise e

            try:
                embds = [[] for _ in range(len(resp["output"]["embeddings"]))]

                for e in resp["output"]["embeddings"]:
                    embds[e["text_index"]] = e["embedding"]
                res.extend(embds)

                token_count += self._total_token_count(resp)
            except Exception as e:
                logging.error(f"Error encoding texts: {e}")
                raise

        return np.array(res), token_count


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
                resp = await asyncio.to_thread(
                    dashscope.TextEmbedding.call,
                    model=self.model_name, 
                    input=text[:2048], 
                    api_key=self.api_key, 
                    text_type="query"
                )
                return np.array(resp["output"]["embeddings"][0]["embedding"]), self._total_token_count(resp)
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Qwen查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Qwen查询编码最终失败: {e}")
                    raise e