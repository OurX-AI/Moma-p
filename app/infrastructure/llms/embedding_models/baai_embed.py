import os
import re
import threading
from typing import List, Tuple
import numpy as np
import asyncio
import logging
from .base import BaseEmbedding
from ..utils import truncate


class BAAIEmbedding(BaseEmbedding):
    _model = None
    _model_lock = threading.Lock()

    def __init__(self, api_key: str, model_provider: str, model_name: str, **kwargs):
        """
        初始化默认嵌入模型
        
        Args:
            api_key (str): API密钥（未使用）
            model_name (str): 模型名称
            **kwargs: 其他参数
            
        注意：
        如果下载HuggingFace模型遇到问题，可以尝试以下方法：
        
        Linux:
        export HF_ENDPOINT=https://hf-mirror.com
        """
        super().__init__(api_key, model_provider, model_name, **kwargs)
 
        with BAAIEmbedding._model_lock:
            logging.info(f"BAAI Embedding model initialized: {model_name}")

            if not BAAIEmbedding._model or model_name != BAAIEmbedding.model_name:
                import torch
                from FlagEmbedding import FlagModel
                from huggingface_hub import snapshot_download
                try:
                    model_path = self._get_model_cache_path(model_name)
                    
                    BAAIEmbedding._model = FlagModel(
                        model_path,
                        query_instruction_for_retrieval="为这个句子生成表示以用于检索相关文章：",
                        use_fp16=torch.cuda.is_available(),
                    )
                    BAAIEmbedding.model_name = model_name
                except Exception:
                    model_path = self._get_model_cache_path(model_name)
                    model_dir = snapshot_download(
                        repo_id="BAAI/bge-large-zh-v1.5", 
                        local_dir=model_path, 
                        local_dir_use_symlinks=False
                    )
                    BAAIEmbedding._model = FlagModel(
                        model_dir, 
                        query_instruction_for_retrieval="为这个句子生成表示以用于检索相关文章：", 
                        use_fp16=torch.cuda.is_available()
                    )
        self._model = BAAIEmbedding._model


    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        if not self._model:
            raise RuntimeError("模型未初始化")
            
        batch_size = 16
        texts = [truncate(t, 2048) for t in texts]

        ress = None
        for i in range(0, len(texts), batch_size):
            if ress is None:
                ress = await asyncio.to_thread(
                    self._model.encode,
                    texts[i : i + batch_size],
                    convert_to_numpy=True
                )
            else:
                batch_embeddings = await asyncio.to_thread(
                    self._model.encode,
                    texts[i : i + batch_size],
                    convert_to_numpy=True
                )
                ress = np.concatenate((ress, batch_embeddings), axis=0)
       
        return ress, self._total_token_count(None, texts)


    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        if not self._model:
            raise RuntimeError("模型未初始化")

        result = await asyncio.to_thread(
            lambda: self._model.encode_queries([text], convert_to_numpy=False)[0][0].cpu().numpy()
        )

        return result, self._total_token_count(None, [text])