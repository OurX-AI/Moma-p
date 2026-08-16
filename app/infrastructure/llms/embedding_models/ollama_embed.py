from typing import List, Tuple
import numpy as np
import asyncio
from ollama import Client
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS


class OllamaEmbed(BaseEmbedding):
    """Ollama嵌入模型实现"""

    _special_tokens = ["<|endoftext|>"]

    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: str, **kwargs):
        """
        初始化Ollama嵌入模型
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (str): API基础URL
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)

        self.client = Client(host=base_url) if not api_key or api_key == "x" else Client(
            host=base_url, 
            headers={"Authorization": f"Bearer {api_key}"}
        )

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

        for text in texts:
            # remove special tokens if they exist
            for token in OllamaEmbed._special_tokens:
                text = text.replace(token, "")

            # 重试逻辑
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    res = await asyncio.to_thread(
                        self.client.embeddings,
                        prompt=text, 
                        model=self.model_name, 
                        options={"use_mmap": True}, 
                        keep_alive=-1
                    )
                    arr.append(res["embedding"])
                    break  # 成功则跳出重试循环
                    
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"Ollama嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"Ollama嵌入编码最终失败: {e}")
                        raise

            token_count += 128
        
        return np.array(arr), token_count

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        # remove special tokens if they exist
        for token in OllamaEmbed._special_tokens:
            text = text.replace(token, "")

        # 重试逻辑
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                res = await asyncio.to_thread(
                    self.client.embeddings,
                    prompt=text, 
                    model=self.model_name, 
                    options={"use_mmap": True}, 
                    keep_alive=-1
                )
                return np.array(res["embedding"]), 128
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Ollama查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Ollama查询编码最终失败: {e}")
                    raise