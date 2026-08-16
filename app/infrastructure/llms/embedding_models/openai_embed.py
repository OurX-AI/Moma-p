from typing import List, Optional, Tuple
import numpy as np
import json
import logging
import asyncio
from urllib.parse import urljoin
from openai import AsyncOpenAI
from openai.lib.azure import AsyncAzureOpenAI
from .base import BaseEmbedding, CONNECTION_TIMEOUT, MAX_RETRY_ATTEMPTS
from ..utils import truncate


class OpenAIEmbed(BaseEmbedding):
    """OpenAI嵌入模型实现"""
    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: Optional[str] = None, **kwargs):
        """
        初始化OpenAI嵌入模型
        
        Args:
            api_key (str): OpenAI API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为text-embedding-ada-002
            base_url (str): API基础URL，默认为OpenAI官方URL
            **kwargs: 其他参数
        """
        if model_provider == "azure":
            key_data = json.loads(api_key)
            api_key_value = key_data.get("api_key", "")
            api_version = key_data.get("api_version", "2024-02-01")
            
            self.client = AsyncAzureOpenAI(
                api_key=api_key_value, 
                azure_endpoint=base_url, 
                api_version=api_version,
                timeout=CONNECTION_TIMEOUT,
                max_retries=MAX_RETRY_ATTEMPTS
            )
        elif model_provider in ["baichuan", "openai"]:
            model_name =  model_name or "text-embedding-ada-002"
            base_url = base_url or "https://api.openai.com/v1"
            self.client = AsyncOpenAI(
                api_key=api_key, 
                base_url=base_url,
                timeout=CONNECTION_TIMEOUT,
                max_retries=MAX_RETRY_ATTEMPTS
            )
        elif model_provider == "xinference":
            # 确保base_url以/v1结尾
            base_url = urljoin(base_url, "v1")
            # 创建OpenAI兼容客户端
            self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            raise ValueError(f"Unsupported model provider: {model_provider}")

        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)
        self._truncate_embed_inputs = model_provider in ("openai", "baichuan", "azure")



    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        # OpenAI要求批次大小<=16
        batch_size = 16
        if self._truncate_embed_inputs:
            texts = [truncate(t, 8191) for t in texts]
        ress = []

        total_tokens = 0
        for i in range(0, len(texts), batch_size):
            # 重试逻辑
            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    res = await self.client.embeddings.create(
                        input=texts[i : i + batch_size], 
                        model=self.model_name
                    )
                    ress.extend([d.embedding for d in res.data])
                    total_tokens += self._total_token_count(res)
                    break  # 成功则跳出重试循环
                    
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"OpenAI嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"OpenAI嵌入编码最终失败: {e}")
                        raise
        
        return np.array(ress), total_tokens

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        if self._truncate_embed_inputs:
            text = truncate(text, 8191)
        # 重试逻辑
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                res = await self.client.embeddings.create(
                    input=text, 
                    model=self.model_name
                )
                return np.array(res.data[0].embedding), self._total_token_count(res)
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"OpenAI查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"OpenAI查询编码最终失败: {e}")
                    raise