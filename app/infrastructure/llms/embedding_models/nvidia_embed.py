from typing import List, Tuple
import numpy as np
import aiohttp
import asyncio
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS


class NvidiaEmbed(BaseEmbedding):
    """NVIDIA嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: str = "https://integrate.api.nvidia.com/v1/embeddings", **kwargs):
        """
        初始化NVIDIA嵌入模型
        
        Args:
            api_key (str): NVIDIA API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (str): API基础URL
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)
        
        # 根据模型名称调整URL和模型名
        if model_name == "nvidia/embed-qa-4":
            self.base_url = "https://ai.api.nvidia.com/v1/retrieval/nvidia/embeddings"
            self.model_name = "NV-Embed-QA"
        elif model_name == "snowflake/arctic-embed-l":
            self.base_url = "https://ai.api.nvidia.com/v1/retrieval/snowflake/arctic-embed-l/embeddings"
        
        self.headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.api_key}",
        }

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
                    async with aiohttp.ClientSession() as session:
                        payload = {
                            "input": texts[i : i + batch_size],
                            "input_type": "query",
                            "model": self.model_name,
                            "encoding_format": "float",
                            "truncate": "END",
                        }
                        
                        async with session.post(self.base_url, headers=self.headers, json=payload) as response:
                            response.raise_for_status()
                            res = await response.json()
                            ress.extend([d["embedding"] for d in res["data"]])

                            token_count += self._total_token_count(res)
                            break  # 成功，跳出重试循环
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"NVIDIA嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"NVIDIA嵌入编码最终失败: {e}")
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
        embds, token_count = await self.encode([text])
        return np.array(embds[0]), token_count