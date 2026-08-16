import base64
import random
from abc import ABC, abstractmethod
from io import BytesIO
from typing import List, Dict, Any, Optional, Tuple, Union
from PIL import Image
import logging
from pathlib import Path
from ..prompts.prompt_template_load import get_prompt_template


# 重试配置常量
MAX_RETRY_ATTEMPTS = 3  # 最大尝试次数
RETRY_DELAY = 2  # 重试间隔（秒）
CONNECTION_TIMEOUT = 30  # 连接超时（秒）

class BaseComputerVision(ABC):
    """计算机视觉模型基类，提供图像描述和视觉聊天功能"""
    
    def __init__(self, api_key: str, model_provider: str, model_name: str, base_url: Optional[str] = None, language: str = "Chinese", **kwargs):
        """
        初始化计算机视觉模型基类
        
        Args:
            api_key (str): API密钥
            model_provider (str): 模型提供商
            model_name (str): 模型名称
            base_url (Optional[str]): API基础URL
            language (str): 语言设置，默认为中文
            kwargs (dict): 其他参数
        """
        self.api_key = api_key
        self.model_provider = model_provider
        self.model_name = model_name
        self.base_url = base_url
        self.language = language
        self.configs = kwargs

        self.default_describe_prompt_zh = "请用中文详细描述一下图中的内容，比如时间，地点，人物，事情，人物心情等，如果有数据请提取出数据。"
        self.default_describe_prompt_en = "Please describe the content of this picture, like where, when, who, what happen. If it has number data, please extract them out."
    
    def _should_retry(self, error: Exception) -> bool:
        """判断异常是否需要重试"""
        error_str = str(error).lower()
        return any(keyword in error_str for keyword in [
            'connection', 'timeout', 'network', 'temporary', 'busy', 'rate limit', 'overload', '429', '503', '502', '504', '500'
        ])
    
    def _get_delay(self, attempt: int = 0) -> float:
        """获取重试延迟时间（指数退避 + 随机抖动）"""
        # 指数退避：2^attempt * 基础延迟
        base_delay = 1.0
        exponential_delay = base_delay * (2 ** attempt)
        
        # 添加随机抖动，避免雷群效应
        jitter = random.uniform(0.5, 1.5)
        
        # 限制最大延迟为30秒
        max_delay = 30.0
        delay = min(exponential_delay * jitter, max_delay)
        
        return delay

    async def describe(self, image: Union[str, bytes, BytesIO, Image.Image]) -> Tuple[str, int]:
        """
        描述图像内容
        
        Args:
            image: 图像对象，可以是文件路径、bytes、BytesIO或PIL Image
            
        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        pass

    async def describe_with_prompt(self, image: Union[str, bytes, BytesIO, Image.Image], prompt: Optional[str] = None) -> Tuple[str, int]:
        """
        使用自定义提示词描述图像
        
        Args:
            image: 图像对象，可以是文件路径、bytes、BytesIO或PIL Image
            prompt (Optional[str]): 自定义提示词
            
        Returns:
            Tuple[str, int]: (图像描述文本, token数量)
        """
        pass

    @abstractmethod
    async def chat(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any], image: str = "") -> Tuple[str, int]:
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
        pass

    @abstractmethod
    async def chat_stream(self, system: str, history: List[Dict[str, Any]], gen_conf: Dict[str, Any], image: str = "") -> Tuple[str, int]:
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
        pass


    def _create_describe_message(self, b64_image: str) -> Dict[str, Any]:
        """
        创建包含图像的消息格式
        
        Args:
            b64_image (str): base64编码的图像
            
        Returns:
            Dict[str, Any]: 消息格式
        """
        return {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                },
                {
                    "type": "text",
                    "text": self.default_describe_prompt_zh if self.language.lower() == "chinese" else self.default_describe_prompt_en,
                },
            ],
        }

    
    def _create_describe_message_with_prompt(self, b64_image: str, prompt: Optional[str] = None) -> Dict[str, Any]:
        """
        获取视觉LLM描述提示词
        
        Args:
            lang (str): 语言设置
            
        Returns:
            str: 视觉LLM提示词
        """

        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                    {
                        "type": "text",
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
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_image}",
                },
            },
            {"type": "text", "text": text},
        ]
    

    def _truncate_response(self, response: str, max_length: int = 1000) -> str:
        """
        截断响应文本并添加截断提示
        
        Args:
            response (str): 响应文本
            max_length (int): 最大字符长度
            
        Returns:
            str: 截断后的文本
        """
        if len(response) <= max_length:
            return response
        
        truncated = response[:max_length]

        return self._add_truncate_notify(truncated)

    def _add_truncate_notify(self, response: str) -> str:
        """
        截断响应文本并添加截断提示
        
        Args:
            response (str): 响应文本            
        Returns:
            str: 截断后的文本
        """
        if self.language.lower() == "chinese":
            response += "······\n由于长度的原因，回答被截断了，要继续吗？"
        else:
            response += "...\nFor the content length reason, it stopped, continue?"
        
        return response


    def _image2base64(self, image: Union[str, bytes, BytesIO, Image.Image]) -> str:
        """
        将图像转换为base64编码字符串
        
        Args:
            image: 图像对象，可以是文件路径、bytes、BytesIO或PIL Image
            
        Returns:
            str: base64编码的图像字符串
        """
        try:
            if isinstance(image, str):
                # 如果是文件路径
                with open(image, 'rb') as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            elif isinstance(image, bytes):
                return base64.b64encode(image).decode("utf-8")
            elif isinstance(image, BytesIO):
                return base64.b64encode(image.getvalue()).decode("utf-8")
            elif isinstance(image, Image.Image):
                buffered = BytesIO()
                try:
                    image.save(buffered, format="JPEG")
                except Exception:
                    image.save(buffered, format="PNG")
                return base64.b64encode(buffered.getvalue()).decode("utf-8")
            else:
                raise ValueError(f"不支持的图像类型: {type(image)}")
        except Exception as e:
            logging.error(f"图像转换为base64失败: {e}")
            raise