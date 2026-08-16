from typing import List, Tuple
import numpy as np
import aiohttp
import asyncio
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS


class HuggingFaceEmbed(BaseEmbedding):
    """HuggingFace嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: str = None, **kwargs):
        """
        初始化HuggingFace嵌入模型
        
        Args:
            api_key (str): HuggingFace API密钥（可选）
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (str): API基础URL，默认为http://127.0.0.1:8080
            **kwargs: 其他参数
        """
        if not model_name:
            raise ValueError("Model name cannot be None")

        # 处理模型名称，移除可能的"___"分隔符
        model_name = model_name.split("___")[0]        
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)


    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        embeddings = []
        
        for text in texts:
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{self.base_url}/embed", 
                            json={"inputs": text}, 
                            headers={"Content-Type": "application/json"}
                        ) as response:
                            if response.status == 200:
                                embedding = await response.json()
                                embeddings.append(embedding[0])
                                break  # 成功，跳出重试循环
                            else:
                                error_text = await response.text()
                                raise Exception(f"Error: {response.status} - {error_text}")
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"HuggingFace嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"HuggingFace嵌入编码最终失败: {e}")
                        raise e
        
        return np.array(embeddings), self._total_token_count(texts=texts)

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
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/embed", 
                        json={"inputs": text}, 
                        headers={"Content-Type": "application/json"}
                    ) as response:
                        if response.status == 200:
                            embedding = await response.json()
                            return np.array(embedding[0]), self._total_token_count(texts=[text])
                        else:
                            error_text = await response.text()
                            raise Exception(f"Error: {response.status} - {error_text}")
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"HuggingFace查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"HuggingFace查询编码最终失败: {e}")
                    raise e