import json
from typing import List, Tuple
import asyncio
import boto3
import numpy as np
import logging
from .base import BaseEmbedding, MAX_RETRY_ATTEMPTS
from ..utils import truncate


class BedrockEmbed(BaseEmbedding):
    """AWS Bedrock嵌入模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str, **kwargs):
        """
        初始化Bedrock嵌入模型
        
        Args:
            api_key (str): AWS凭证JSON字符串，包含bedrock_ak, bedrock_sk, bedrock_region
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, **kwargs)
    
        # 解析AWS凭证
        try:
            key_data = json.loads(api_key)
            self.bedrock_ak = key_data.get("bedrock_ak", "")
            self.bedrock_sk = key_data.get("bedrock_sk", "")
            self.bedrock_region = key_data.get("bedrock_region", "")
        except (json.JSONDecodeError, TypeError):
            # 如果解析失败，使用空值，将依赖默认凭证
            self.bedrock_ak = ""
            self.bedrock_sk = ""
            self.bedrock_region = ""
        
        # 创建Bedrock客户端
        if self.bedrock_ak == "" or self.bedrock_sk == "" or self.bedrock_region == "":
            # 使用默认凭证 (AWS_PROFILE, AWS_DEFAULT_REGION, etc.)
            self.client = boto3.client("bedrock-runtime")
        else:
            self.client = boto3.client(
                service_name="bedrock-runtime", 
                region_name=self.bedrock_region, 
                aws_access_key_id=self.bedrock_ak, 
                aws_secret_access_key=self.bedrock_sk
            )

    async def encode(self, texts: List[str]) -> Tuple[np.ndarray, int]:
        """
        将文本列表编码为嵌入向量
        
        Args:
            texts (List[str]): 待编码的文本列表
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量数组, token总数)
        """
        texts = [truncate(t, 8196) for t in texts]
        embeddings = []
        token_count = 0
        
        for text in texts:
            # 根据模型类型构建请求体
            if self.model_name.split(".")[0] == "amazon":
                body = {"inputText": text}
            elif self.model_name.split(".")[0] == "cohere":
                body = {"texts": [text], "input_type": "search_document"}

            for attempt in range(MAX_RETRY_ATTEMPTS):
                try:
                    response = await asyncio.to_thread(
                        self.client.invoke_model,
                        modelId=self.model_name, 
                        body=json.dumps(body)
                    )
                    model_response = json.loads(response["body"].read())
                    embeddings.extend([model_response["embedding"]])

                    token_count += self._total_token_count(texts=[text])
                    break  # 成功，跳出重试循环
                except Exception as e:
                    if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                        delay = self._get_delay(attempt)
                        logging.warning(f"Bedrock嵌入编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logging.error(f"Bedrock嵌入编码最终失败: {e}")
                        raise e
        
        return np.array(embeddings), token_count

    async def encode_queries(self, text: str) -> Tuple[np.ndarray, int]:
        """
        将查询文本编码为嵌入向量
        
        Args:
            text (str): 待编码的查询文本
            
        Returns:
            Tuple[np.ndarray, int]: (嵌入向量, token总数)
        """
        embeddings = []
        token_count = self._total_token_count(texts=[text])
        
        # 根据模型类型构建请求体
        if self.model_name.split(".")[0] == "amazon":
            body = {"inputText": truncate(text, 8196)}
        elif self.model_name.split(".")[0] == "cohere":
            body = {"texts": [truncate(text, 8196)], "input_type": "search_query"}

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.invoke_model,
                    modelId=self.model_name, 
                    body=json.dumps(body)
                )
                model_response = json.loads(response["body"].read())
                embeddings.extend(model_response["embedding"])
                break  # 成功，跳出重试循环
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._is_retryable_error(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Bedrock查询编码失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Bedrock查询编码最终失败: {e}")
                    raise e

        return np.array(embeddings), token_count