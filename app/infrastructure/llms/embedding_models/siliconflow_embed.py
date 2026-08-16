import logging
from typing import List, Tuple
import numpy as np
import aiohttp
import asyncio
import urllib.parse
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS


class SILICONFLOWEmbed(BaseEmbedding):
    """SiliconFlow嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: str = "https://api.siliconflow.cn/v1/embeddings", **kwargs):
        """
        初始化SiliconFlow嵌入模型
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (str): API基础URL，默认为SiliconFlow官方URL
            **kwargs: 其他参数（如description等）
        """
        # 如果 base_url 不包含 "embeddings"，则添加
        if not base_url.endswith("embeddings"):
            base_url = urllib.parse.urljoin(base_url, "embeddings")

        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)

        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        }
        dims = self.configs.get("dimensions")
        self._dimensions = int(dims) if dims is not None else None

    @staticmethod
    def _batch_len_info(texts: List[str]) -> str:
        lengths = [len(str(t or "")) for t in texts]
        empty_idxs = [i for i, t in enumerate(texts) if not str(t or "").strip()]
        max_chars = max(lengths) if lengths else 0
        return f"count={len(texts)} char_lens={lengths} empty_idxs={empty_idxs} max_chars={max_chars}"

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

        async with aiohttp.ClientSession() as session:
            for i in range(0, len(texts), batch_size):
                texts_batch = texts[i : i + batch_size]
                payload = {
                    "model": self.model_name,
                    "input": texts_batch,
                    "encoding_format": "float",
                }
                if self._dimensions is not None:
                    payload["dimensions"] = self._dimensions

                # 重试逻辑
                for attempt in range(MAX_RETRY_ATTEMPTS):
                    try:
                        async with session.post(self.base_url, json=payload, headers=self.headers) as response:
                            res = await response.json()
                            if not res or not res.get("data"):
                                raise ValueError(f"Invalid API response: {res}")
                            ress.extend([d["embedding"] for d in res["data"] if d and "embedding" in d])
                            token_count += self._total_token_count(res)
                            break  # 成功则跳出重试循环
                            
                    except Exception as e:
                        if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                            delay = self._get_delay(attempt)
                            logging.warning(
                                "SiliconFlow嵌入编码失败，重试 (尝试 %s/%s): %s. 等待 %.2fs... | model=%s | %s",
                                attempt + 1,
                                MAX_RETRY_ATTEMPTS,
                                e,
                                delay,
                                self.model_name,
                                self._batch_len_info(texts_batch),
                            )
                            await asyncio.sleep(delay)
                            continue
                        logging.error(
                            "SiliconFlow嵌入编码最终失败: %s | model=%s | %s",
                            e,
                            self.model_name,
                            self._batch_len_info(texts_batch),
                        )
                        raise

        return np.array(ress), token_count

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        payload = {
            "model": self.model_name,
            "input": text,
            "encoding_format": "float",
        }
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions

        # 重试逻辑
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.base_url, json=payload, headers=self.headers) as response:
                        res = await response.json()
                        if not res or not res.get("data") or not res["data"]:
                            raise ValueError(f"Invalid API response: {res}")
                        return np.array(res["data"][0]["embedding"]), self._total_token_count(res)

            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(
                        "SiliconFlow查询编码失败，重试 (尝试 %s/%s): %s. 等待 %.2fs... | model=%s | %s",
                        attempt + 1,
                        MAX_RETRY_ATTEMPTS,
                        e,
                        delay,
                        self.model_name,
                        self._batch_len_info([text]),
                    )
                    await asyncio.sleep(delay)
                    continue
                logging.error(
                    "SiliconFlow查询编码最终失败: %s | model=%s | %s",
                    e,
                    self.model_name,
                    self._batch_len_info([text]),
                )
                raise
