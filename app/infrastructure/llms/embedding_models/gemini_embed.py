from typing import List, Tuple
import asyncio
import numpy as np
import logging
from google import genai
from google.genai import types
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS
from ..utils import truncate


class GeminiEmbed(BaseEmbedding):
    """Google Gemini嵌入模型实现（google-genai 新 SDK）"""

    def __init__(self, api_key: str, model_provider: str, model_name: str = "text-embedding-004", **kwargs):
        """
        初始化Gemini嵌入模型

        Args:
            api_key (str): Google API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为text-embedding-004
            **kwargs: 其他参数
        """
        if not model_name.startswith("models/"):
            model_name = "models/" + model_name
        super().__init__(api_key, model_provider, model_name, **kwargs)
        self.client = genai.Client(api_key=api_key)

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量

        Args:
            texts (List[str]): 待编码的文本列表

        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        texts = [truncate(t, 2048) for t in texts]
        batch_size = 16
        ress = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    response = await asyncio.to_thread(
                        self.client.models.embed_content,
                        model=self.model_name,
                        contents=batch,
                        config=types.EmbedContentConfig(
                            task_type="RETRIEVAL_DOCUMENT",
                            title="Embedding of single string",
                        ),
                    )
                    for emb in response.embeddings:
                        ress.append(emb.values)
                    break
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"Gemini嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"Gemini嵌入编码最终失败: {e}")
                        raise e

        return np.array(ress), self._total_token_count(texts=texts)

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
                response = await asyncio.to_thread(
                    self.client.models.embed_content,
                    model=self.model_name,
                    contents=truncate(text, 2048),
                    config=types.EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT",
                        title="Embedding of single string",
                    ),
                )
                return np.array(response.embeddings[0].values), self._total_token_count(texts=[text])
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Gemini查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Gemini查询编码最终失败: {e}")
                    raise e
