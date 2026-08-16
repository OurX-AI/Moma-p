import base64
import logging
from typing import List, Dict, Any, Optional, Tuple, Union
from io import BytesIO
from PIL.Image import open as pil_open
import asyncio
from pathlib import Path
from google import genai
from google.genai import types
from .base import BaseComputerVision, MAX_RETRY_ATTEMPTS
from ..prompts.prompt_template_load import get_prompt_template


class GeminiCV(BaseComputerVision):
    """Google Gemini 计算机视觉模型实现（google-genai 新 SDK）"""

    def __init__(self, api_key: str, model_provider: str, model_name: str = "gemini-1.0-pro-vision-latest",
                 base_url: Optional[str] = None, language: str = "Chinese"):
        """
        初始化Google Gemini计算机视觉模型

        Args:
            api_key (str): Google API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称，默认为gemini-1.0-pro-vision-latest
            base_url (Optional[str]): API基础URL
            language (str): 语言设置
        """
        super().__init__(api_key, model_provider, model_name, base_url, language)
        self.client = genai.Client(api_key=api_key)

    async def describe(self, image: Union[str, bytes, BytesIO, Any]) -> Tuple[str, int]:
        """
        描述图像内容

        Args:
            image: 图像对象或路径

        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        message = self._create_describe_message("")
        b64 = self._image2base64(image)
        img_bytes = base64.b64decode(b64)
        img_part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
        text_part = types.Part.from_text(text=message["content"][1]["text"])
        contents = [text_part, img_part]

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model_name,
                    contents=contents,
                )
                total_tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
                return response.text or "", total_tokens
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
        vision_prompt = prompt or get_prompt_template(
            str(Path(__file__).parent.parent / "prompts" / "cv"),
            "vision_llm_describe_prompt.md",
            {"page": None}
        )
        img_bytes = base64.b64decode(b64)
        img_part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
        text_part = types.Part.from_text(text=vision_prompt)
        contents = [text_part, img_part]

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model_name,
                    contents=contents,
                )
                total_tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
                return response.text or "", total_tokens
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
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        contents_list = []
        for his in history:
            role = "model" if his["role"] == "assistant" else "user"
            parts = [types.Part.from_text(text=his["content"])]
            if his == history[-1] and image:
                img_bytes = base64.b64decode(image) if "base64," not in image else base64.b64decode(image.split(",", 1)[1])
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
            contents_list.append(types.Content(role=role, parts=parts))

        config = types.GenerateContentConfig(
            temperature=gen_conf.get("temperature", 0.3),
            top_p=gen_conf.get("top_p", 0.7),
        )

        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model_name,
                    contents=contents_list,
                    config=config,
                )
                total_tokens = response.usage_metadata.total_token_count if response.usage_metadata else 0
                return response.text or "", total_tokens
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
        if system:
            history[-1]["content"] = system + history[-1]["content"] + "user query: " + history[-1]["content"]

        contents_list = []
        for his in history:
            role = "model" if his["role"] == "assistant" else "user"
            parts = [types.Part.from_text(text=his["content"])]
            if his == history[-1] and image:
                img_bytes = base64.b64decode(image) if "base64," not in image else base64.b64decode(image.split(",", 1)[1])
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
            contents_list.append(types.Content(role=role, parts=parts))

        config = types.GenerateContentConfig(
            temperature=gen_conf.get("temperature", 0.3),
            top_p=gen_conf.get("top_p", 0.7),
        )

        ans = ""
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                def _consume_stream():
                    return list(self.client.models.generate_content_stream(
                        model=self.model_name,
                        contents=contents_list,
                        config=config,
                    ))

                chunks = await asyncio.to_thread(_consume_stream)
                for chunk in chunks:
                    if chunk.text:
                        ans += chunk.text
                    token_count = chunk.usage_metadata.total_token_count if chunk.usage_metadata else 0
                    yield ans, token_count
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
