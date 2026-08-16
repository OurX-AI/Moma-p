import io
import os
import uuid
import logging
from http import HTTPStatus
from typing import List, Dict, Any, Optional, Tuple, Union
from io import BytesIO
from PIL import Image
import asyncio
import dashscope
from pathlib import Path
from dashscope import MultiModalConversation
from .base import BaseComputerVision, MAX_RETRY_ATTEMPTS
from ..prompts.prompt_template_load import get_prompt_template
from app.config.settings import settings


class QWenCV(BaseComputerVision):
    """通义千问计算机视觉模型实现"""

    def __init__(self, api_key: str, model_provider: str, model_name: str = "qwen-vl-chat-v1", 
                 base_url: str = None, language: str = "Chinese", **kwargs):
        """ 
        初始化通义千问计算机视觉模型
        
        Args:
            api_key (str): 通义千问API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为qwen-vl-chat-v1
            base_url (str): API基础URL
            language (str): 语言设置
        """
        super().__init__(api_key, model_provider, model_name, base_url, language, **kwargs)
        
        dashscope.api_key = api_key

    async def describe(self, image: Union[str, bytes, BytesIO, Any]) -> Tuple[str, int]:
        """
        描述图像内容
        
        Args:
            image: 图像对象或路径
            
        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        message = self._create_describe_message(image)
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    lambda: MultiModalConversation.call(model=self.model_name, messages=message)
                )
                
                if response.status_code == HTTPStatus.OK:
                    return response.output.choices[0]["message"]["content"][0]["text"], response.usage.output_tokens
                else:
                    # 非HTTP错误，抛出异常
                    raise Exception(f"**ERROR**: {response.message}")

            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Qwen图像描述失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Qwen图像描述最终失败: {e}")
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
        message = self._create_describe_message_with_prompt(image, prompt)
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    lambda: MultiModalConversation.call(model=self.model_name, messages=message)
                )
                
                if response.status_code == HTTPStatus.OK:
                    return response.output.choices[0]["message"]["content"][0]["text"], response.usage.output_tokens
                else:
                    # 非HTTP错误，抛出异常
                    raise Exception(f"**ERROR**: {response.message}")

            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Qwen自定义提示词图像描述失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Qwen自定义提示词图像描述最终失败: {e}")
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
                    his["content"] = self._create_chat_message(his["content"], image)
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    lambda: MultiModalConversation.call(
                        model=self.model_name,
                        messages=history,
                        temperature=gen_conf.get("temperature", 0.3),
                        top_p=gen_conf.get("top_p", 0.7),
                    )
                )

                answer = ""
                tk_count = 0
                if response.status_code == HTTPStatus.OK:
                    answer = response.output.choices[0]["message"]["content"]
                    if isinstance(answer, list):
                        answer = answer[0]["text"] if answer else ""
                    tk_count += response.usage.total_tokens
                    if response.output.choices[0].get("finish_reason", "") == "length":
                        answer = self._add_truncate_notify(answer)
                    return answer, tk_count
                else:
                    # 非HTTP错误，抛出异常
                    raise Exception(f"**ERROR**: {response.message}")

            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Qwen视觉聊天失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Qwen视觉聊天最终失败: {e}")
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
                    his["content"] = self._create_chat_message(his["content"], image)

        answer = ""
        tk_count = 0
        
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    lambda: MultiModalConversation.call(
                        model=self.model_name,
                        messages=history,
                        temperature=gen_conf.get("temperature", 0.3),
                        top_p=gen_conf.get("top_p", 0.7),
                        stream=True,
                    )
                )
                
                for resp in response:
                    if resp.status_code == HTTPStatus.OK:
                        content = resp.output.choices[0]["message"]["content"]
                        if isinstance(content, list):
                            content = content[0]["text"] if content else ""
                        answer += content
                        token_count = resp.usage.total_tokens
                        if resp.output.choices[0].get("finish_reason", "") == "length":
                            answer = self._add_truncate_notify(answer)
                        yield answer, token_count
                    else:
                        error_msg = resp.message if str(resp.message).find("Access") < 0 else "Out of credit. Please set the API key in **settings > Model providers.**"
                        raise Exception(f"**ERROR**: {error_msg}")

                # 如果成功完成流式响应，跳出重试循环
                return

            except Exception as e:
                if attempt < MAX_RETRY_ATTEMPTS - 1 and self._should_retry(e):
                    delay = self._get_delay(attempt)
                    logging.warning(f"Qwen流式视觉聊天失败，重试 (尝试 {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. 等待 {delay:.2f}s...")
                    await asyncio.sleep(delay)
                    continue
                else:
                    logging.error(f"Qwen流式视觉聊天最终失败: {e}")
                    raise Exception(f"**ERROR**: {e}")

    
    def _create_describe_message(self, b64_image: str) -> Dict[str, Any]:
        """
        创建包含图像的消息格式
        
        Args:
            b64_image (str): base64编码的图像
            
        Returns:
            Dict[str, Any]: 消息格式
        """
        tmp_dir = settings.model_temp_dir
        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, "%s.jpg" % uuid.uuid1().hex())
        Image.open(io.BytesIO(b64_image)).save(path)

        return [
            {
                "role": "user",
                "content": [
                    {"image": f"file://{path}"},
                    {
                        "text": self.default_describe_prompt_zh if self.language.lower() == "chinese" else self.default_describe_prompt_en,
                    },
                ],
            }
        ]

    
    def _create_describe_message_with_prompt(self, b64_image: str, prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        获取视觉LLM描述提示词
        
        Args:
            lang (str): 语言设置
            
        Returns:
            str: 视觉LLM提示词
        """

        tmp_dir = settings.model_temp_dir
        if not os.path.exists(tmp_dir):
            os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, "%s.jpg" % uuid.uuid1().hex())
        Image.open(io.BytesIO(b64_image)).save(path)

        return [
            {
                "role": "user",
                "content": [
                    {"image": f"file://{path}"},
                    {
                        "text": prompt if prompt else get_prompt_template(
                            str(Path(__file__).parent.parent / "prompts" / "cv"),
                            "computer_vision_describe_prompt.md",
                            {"page": None}
                        ),
                    },
                ],
            }
        ]


    def _create_chat_message(self, text: str, b64_image: str) -> List[Dict[str, Any]]:
        """
        创建聊天消息格式
        
        Args:
            text (str): 文本内容
            b64_image (str): base64编码的图像
            
        Returns:
            List[Dict[str, Any]]: 消息格式列表
        """
        return [
            {"image": f"{b64_image}"},
            {"text": text},
        ]