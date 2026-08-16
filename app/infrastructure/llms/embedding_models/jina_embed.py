from __future__ import annotations
import asyncio
import logging
import urllib.parse
from typing import List, Optional, Tuple
import aiohttp
import numpy as np
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS

class JinaEmbed(BaseEmbedding):
    """Jina AI 嵌入模型（OpenAI 兼容 /embeddings；v3/v4 支持 task）。"""

    _TASK_AWARE_PREFIXES = (
        "jina-embeddings-v3",
        "jina-embeddings-v4",
        "jina-clip-v",
    )

    def __init__(
        self,
        api_key: str,
        model_provider: str,
        model_name: str,
        base_url: str = "https://api.jina.ai/v1/embeddings",
        **kwargs,
    ):
        if not (base_url or "").rstrip("/").endswith("embeddings"):
            base_url = urllib.parse.urljoin(base_url or "https://api.jina.ai/v1/", "embeddings")
        super().__init__(api_key, model_provider, model_name, base_url, **kwargs)
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        }
        self._document_task = str(self.configs.get("task_document") or "retrieval.passage")
        self._query_task = str(self.configs.get("task_query") or "retrieval.query")
        dims = self.configs.get("dimensions")
        self._dimensions: Optional[int] = int(dims) if dims is not None else None

    def _task_aware(self) -> bool:
        name = (self.model_name or "").strip().lower()
        return any(name.startswith(p) for p in self._TASK_AWARE_PREFIXES)

    def _build_payload(self, texts: List[str], *, task: Optional[str]) -> dict:
        payload: dict = {
            "model": self.model_name,
            "input": texts,
            "embedding_type": "float",
        }
        if self._task_aware() and task:
            payload["task"] = task
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions
        return payload

    @staticmethod
    def _batch_len_info(texts: List[str]) -> str:
        lengths = [len(str(t or "")) for t in texts]
        empty_idxs = [i for i, t in enumerate(texts) if not str(t or "").strip()]
        max_chars = max(lengths) if lengths else 0
        return f"count={len(texts)} char_lens={lengths} empty_idxs={empty_idxs} max_chars={max_chars}"

    async def _post_embeddings(self, texts: List[str], *, task: Optional[str]) -> Tuple[np.ndarray, int]:
        batch_size = 16
        ress: List[List[float]] = []
        token_count = 0
        async with aiohttp.ClientSession() as session:
            for i in range(0, len(texts), batch_size):
                texts_batch = texts[i : i + batch_size]
                payload = self._build_payload(texts_batch, task=task)
                for attempt in range(MAX_RETRY_ATTEMPTS):
                    try:
                        async with session.post(
                            self.base_url, json=payload, headers=self.headers
                        ) as response:
                            res = await response.json()
                            if not res or not res.get("data"):
                                raise ValueError(f"Invalid API response: {res}")
                            ordered = sorted(res["data"], key=lambda d: int(d.get("index", 0)))
                            ress.extend(
                                [d["embedding"] for d in ordered if d and "embedding" in d]
                            )
                            token_count += self._total_token_count(res)
                            break
                    except Exception as e:
                        if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                            delay = self._get_delay(attempt)
                            logging.warning(
                                "Jina嵌入编码失败，重试 (尝试 %s/%s): %s. 等待 %.2fs... | model=%s | %s",
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
                            "Jina嵌入编码最终失败: %s | model=%s | %s",
                            e,
                            self.model_name,
                            self._batch_len_info(texts_batch),
                        )
                        raise
        return np.array(ress), token_count

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """文档/批量编码：task-aware 模型默认 retrieval.passage。"""
        task = self._document_task if self._task_aware() else None
        return await self._post_embeddings(texts, task=task)

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """查询编码：task-aware 模型默认 retrieval.query。"""
        task = self._query_task if self._task_aware() else None
        vecs, tokens = await self._post_embeddings([text], task=task)
        return np.array(vecs[0]), tokens
