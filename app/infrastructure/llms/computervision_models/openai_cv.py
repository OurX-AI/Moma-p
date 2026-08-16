from typing import List, Dict, Any, Optional, Tuple, Union
from io import BytesIO
import json
import asyncio
import logging
from openai import AsyncOpenAI
from openai.lib.azure import AsyncAzureOpenAI
from .base import BaseComputerVision, MAX_RETRY_ATTEMPTS, CONNECTION_TIMEOUT


class OpenAICV(BaseComputerVision):
    """OpenAI兼容的计算机视觉模型基类，包含公共逻辑"""
    
    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: Optional[str] = None, language: str = "Chinese", **kwargs):
        """
        初始化OpenAI兼容的计算机视觉模型
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (Optional[str]): API基础URL
            language (str): 语言设置
        """
        if model_provider == "openai":
            model_name =  model_name or "gpt-4-vision-preview"
            base_url = base_url or "https://api.openai.com/v1"

            # 配置客户端，添加超时和重试设置
            self.client = AsyncOpenAI(
                api_key=api_key, 
                base_url=base_url,
                timeout=CONNECTION_TIMEOUT,
                max_retries=MAX_RETRY_ATTEMPTS
            )
        elif model_provider == "azure":
            if not base_url:
                raise ValueError("Azure OpenAI base_url 不能为空")
            try:
                key_config = json.loads(api_key)
                api_key_value = key_config.get("api_key", "")
                api_version = key_config.get("api_version", "2024-02-01")
                
                self.client = AsyncAzureOpenAI(
                    api_key=api_key_value, 
                    azure_endpoint=base_url, 
                    api_version=api_version,
                    timeout=CONNECTION_TIMEOUT,  # 使用统一超时配置
                    max_retries=MAX_RETRY_ATTEMPTS  # 最多重试3次
                )
            except json.JSONDecodeError:
                # 如果不是JSON格式，直接使用api_key
                self.client = AsyncAzureOpenAI(
                    api_key=api_key, 
                    azure_endpoint=base_url, 
                    api_version="2024-02-01",
                    timeout=CONNECTION_TIMEOUT,  # 使用统一超时配置
                    max_retries=MAX_RETRY_ATTEMPTS  # 最多重试3次
                )
        elif model_provider == "siliconflow":
            model_name =  model_name or "Qwen/Qwen2-VL-7B-Instruct"
            base_url = base_url or "https://api.siliconflow.cn/v1"
            # 配置客户端，添加超时和重试设置
            self.client = AsyncOpenAI(
                api_key=api_key, 
                base_url=base_url,
                timeout=CONNECTION_TIMEOUT, 
                max_retries=MAX_RETRY_ATTEMPTS  
            )
        else:
            raise ValueError(f"Unsupported model provider: {model_provider}")

        super().__init__(api_key, model_provider, model_name, base_url, language, **kwargs)

    async def describe(self, image: Union[str, bytes, BytesIO, Any]) -> Tuple[str, int]:
        """
        描述图像内容
        
        Args:
            image: 图像对象或路径
            
        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        b64 = self._image2base64(image)
        message = self._create_describe_message(b64)
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[message],
                )
                
                return response.choices[0].message.content.strip(), response.usage.total_tokens
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"图像描述失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"图像描述最终失败: {e}")
                    raise Exception(f"**ERROR**: {e}")

    async def describe_with_prompt(self, image: Union[str, bytes, BytesIO, Any], 
                            prompt: Optional[str] = None) -> Tuple[str, int]:
        """
        使用自定义提示词描述图像
        
        Args:
            image: 图像对象或路径
            prompt (Optional[str]): 自定义提示词
            
        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        b64 = self._image2base64(image)
        vision_message = self._create_describe_message_with_prompt(b64, prompt)
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=vision_message,
                )
                
                return response.choices[0].message.content.strip(), response.usage.total_tokens
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"自定义提示词图像描述失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"自定义提示词图像描述最终失败: {e}")
                    raise Exception(f"**ERROR**: {e}")

    async def chat(self, system: str, history: List[Dict[str, Any]], 
             gen_conf: Dict[str, Any], image: str = "") -> Tuple[str, int]:
        """
        执行视觉聊天对话
        
        Args:
            system (str): 系统提示词
            history (List[Dict[str, Any]]): 对话历史
            gen_conf (Dict[str, Any]): 生成配置
            image (str): 图像内容（base64编码）
            
        Returns:
            Tuple[str, int]: (回答内容, token数量)
        """
        if system and history:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]
        
        for his in history:
            if his["role"] == "user":
                his["content"] = self._create_chat_message(his["content"], image)

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=history,
                    temperature=gen_conf.get("temperature", 0.3),
                    top_p=gen_conf.get("top_p", 0.7),
                )
                return response.choices[0].message.content.strip(), response.usage.total_tokens
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"视觉聊天失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"视觉聊天最终失败: {e}")
                    raise Exception(f"**ERROR**: {e}")

    async def chat_stream(self, system: str, history: List[Dict[str, Any]], 
                     gen_conf: Dict[str, Any], image: str = "") -> Tuple[str, int]:
        """
        执行流式视觉聊天对话
        
        Args:
            system (str): 系统提示词
            history (List[Dict[str, Any]]): 对话历史
            gen_conf (Dict[str, Any]): 生成配置
            image (str): 图像内容（base64编码）
            
        Yields:
            Tuple[str, int]: (流式响应内容, token数量)
        """
        if system and history:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        for his in history:
            if his["role"] == "user":
                his["content"] = self._create_chat_message(his["content"], image)

        answer = ""
        token_count = 0
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=history,
                    temperature=gen_conf.get("temperature", 0.3),
                    top_p=gen_conf.get("top_p", 0.7),
                    stream=True,
                )
                
                async for resp in response:
                    if not resp.choices[0].delta.content:
                        continue
                    delta = resp.choices[0].delta.content
                    answer += delta
                    
                    # 更新token数量（如果响应中包含usage信息）
                    if hasattr(resp, 'usage') and resp.usage and resp.usage.total_tokens:
                        token_count = resp.usage.total_tokens
                    
                    if resp.choices[0].finish_reason == "length":
                        answer = self._add_truncate_notify(answer)
                    if resp.choices[0].finish_reason in ["stop", "length"]:
                        if hasattr(resp, 'usage') and resp.usage and resp.usage.total_tokens:
                            token_count = resp.usage.total_tokens
                
                yield answer, token_count
                
                # 如果成功完成流式响应，跳出重试循环
                return
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"流式视觉聊天失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"流式视觉聊天最终失败: {e}")
                    raise Exception(f"**ERROR**: {e}")