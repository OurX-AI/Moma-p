from typing import List, Dict, Any, Optional, Tuple, Union
from io import BytesIO
import asyncio
import logging
from ollama import Client
from .base import BaseComputerVision, MAX_RETRY_ATTEMPTS


class OllamaCV(BaseComputerVision):
    """Ollama 计算机视觉模型实现"""

    def __init__(self, api_key: str = "", model_provider: str = "ollama", model_name: str = "llava", 
                 base_url: str = "http://localhost:11434", language: str = "Chinese"):
        """
        初始化Ollama计算机视觉模型
        
        Args:
            api_key (str): API密钥（Ollama通常不需要）
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为llava
            base_url (Optional[str]): Ollama服务地址
            language (str): 语言设置
        """
        super().__init__(api_key, model_provider, model_name, base_url, language)
        
        self.client = Client(host=base_url)

    async def describe(self, image: Union[str, bytes, BytesIO, Any]) -> Tuple[str, int]:
        """
        描述图像内容
        
        Args:
            image: 图像对象或路径
            
        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        # 本地处理，不需要重试
        message = self._create_describe_message("")

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.generate,
                    model=self.model_name,
                    prompt=message[0]["content"][1]["text"],
                    images=[image],
                )

                ans = response["response"].strip()
                return ans, 128  # Ollama通常不返回准确的token数量
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Ollama图像描述失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Ollama图像描述最终失败: {e}")
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
        vision_message = self._create_describe_message_with_prompt("", prompt)
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.generate,
                    model=self.model_name,
                    prompt=vision_message[0]["content"][1]["text"],
                    images=[image],
                )

                ans = response["response"].strip()
                return ans, 128  # Ollama通常不返回准确的token数量
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Ollama自定义提示词图像描述失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Ollama自定义提示词图像描述最终失败: {e}")
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
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        for his in history:
            if his["role"] == "user":
                his["images"] = [image]
        
        options = {}
        if "temperature" in gen_conf:
            options["temperature"] = gen_conf["temperature"]
        if "top_p" in gen_conf:
            options["top_k"] = gen_conf["top_p"]
        if "presence_penalty" in gen_conf:
            options["presence_penalty"] = gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            options["frequency_penalty"] = gen_conf["frequency_penalty"]
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.chat,
                    model=self.model_name,
                    messages=history,
                    options=options,
                    keep_alive=-1,
                )

                ans = response["message"]["content"].strip()
                return ans, response["eval_count"] + response.get("prompt_eval_count", 0)
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Ollama视觉聊天失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Ollama视觉聊天最终失败: {e}")
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
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        for his in history:
            if his["role"] == "user":
                his["images"] = [image]
        
        options = {}
        if "temperature" in gen_conf:
            options["temperature"] = gen_conf["temperature"]
        if "top_p" in gen_conf:
            options["top_k"] = gen_conf["top_p"]
        if "presence_penalty" in gen_conf:
            options["presence_penalty"] = gen_conf["presence_penalty"]
        if "frequency_penalty" in gen_conf:
            options["frequency_penalty"] = gen_conf["frequency_penalty"]
        
        ans = ""
        token_count = 0
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.chat,
                    model=self.model_name,
                    messages=history,
                    stream=True,
                    options=options,
                    keep_alive=-1,
                )
                
                for resp in response:
                    if resp["done"]:
                        token_count = resp.get("prompt_eval_count", 0) + resp.get("eval_count", 0)
                    ans += resp["message"]["content"]
                    yield ans, token_count
                
                # 如果成功完成流式响应，跳出重试循环
                return
                
            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Ollama流式视觉聊天失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Ollama流式视觉聊天最终失败: {e}")
                    raise Exception(f"**ERROR**: {e}")
