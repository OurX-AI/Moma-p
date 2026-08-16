import json
import re
from enum import Enum
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field
from datetime import datetime


class Role(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class Function(BaseModel):
    name: str
    arguments: Dict[str,Any]

    def model_dump(self) -> Dict[str, Any]:
        return {"name": self.name,"arguments": self.arguments}

class ToolCall(BaseModel):
    """助手发起的单次工具调用（assistant 消息中）。"""
    id: str
    type: str = "function"
    function: Function

    def model_dump(self) -> Dict[str, Any]:
        """自定义序列化方法"""
        return {
            "id": self.id,
            "type": self.type,
            "function": self.function.model_dump()
        }

def _strip_ansi(text: str) -> str:
    """移除终端 ANSI 转义序列（颜色、样式等），避免在网页/非终端中显示为乱码。"""
    if not text:
        return text
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _looks_like_code(text: str) -> bool:
    """粗略判断内容是否像代码（多行或含常见代码特征）。"""
    if not text or not text.strip():
        return False
    t = text.strip()
    if "\n" in t and t.count("\n") >= 1:
        return True
    code_marks = ("def ", "class ", "import ", "from ", "return ", "if __name__", "=>", "function ", "const ", "let ")
    return any(m in t for m in code_marks)


class Message(BaseModel):
    """聊天消息，兼容 OpenAI 风格。按用途分三种形态：

    - 普通消息：role + content（system/user/assistant 的普通回复）
    - 助手工具调用：role="assistant" + tool_calls（可选 content）
    - 工具执行结果：role="tool" + tool_result + content
    """
    role: Role
    content: str = ""

    # 模型返回的工具调用信息记录
    tool_calls: Optional[List[ToolCall]] = Field(default=None, description="助手发起的工具调用列表，仅 role=assistant 时使用")

    # 工具执行结果信息
    name: Optional[str] = Field(default=None, description="工具名")
    tool_call_id: Optional[str] = Field(default=None, description="对应 assistant 消息里 tool_calls[].id")
    success: Optional[bool] = Field(default=None, description="工具执行是否成功")
    tool_params: Optional[Dict[str, Any]] = Field(default=None, description="工具调用参数摘要（仅 UI 展示用，不进 LLM context）")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="工具结果元数据：truncated、outputPath 等，供人/UI 用，不发给模型")

    create_time: Optional[datetime] = Field(default=None)

    @property
    def is_tool_result(self) -> bool:
        """是否为工具执行结果消息（role=tool 且带 tool_result）。"""
        return self.role == Role.TOOL and self.name is not None and self.tool_call_id is not None

    @property
    def is_assistant_tool_calls(self) -> bool:
        """是否为助手工具调用消息（role=assistant 且带 tool_calls）。"""
        return self.role == Role.ASSISTANT and bool(self.tool_calls)

    @classmethod
    def system_message(cls, content: str) -> "Message":
        """创建系统消息"""
        return cls(role="system", content=content)

    @classmethod
    def user_message(cls, content: str) -> "Message":
        """创建用户消息"""
        return cls(role="user", content=content, create_time=datetime.now())

    @classmethod
    def assistant_message(cls, content: Optional[str] = None) -> "Message":
        """创建助手消息"""
        return cls(role="assistant", content=content, create_time=datetime.now())

    @classmethod
    def tool_call_message(cls, content: Union[str, List[str]] = "", tool_calls: Optional[List[ToolCall]] = None, **kwargs) -> "Message":
        """创建带工具调用的助手消息。"""
        return cls(
            role="assistant", content=content, tool_calls=tool_calls, create_time=datetime.now(), **kwargs
        )

    @classmethod
    def tool_result_message(
        cls,
        content: str,
        name: str,
        tool_call_id: str,
        success: bool,
        metadata: Optional[Dict[str, Any]] = None,
        tool_params: Optional[Dict[str, Any]] = None,
    ) -> "Message":
        """创建工具执行结果消息（role=tool）。content 为进上下文的摘要；metadata 含 truncated/outputPath 供人/UI。"""
        return cls(
            role="tool",
            content=content,
            name=name,
            tool_call_id=tool_call_id,
            success=success,
            tool_params=tool_params,
            metadata=metadata,
            create_time=datetime.now(),
        )

    def to_json(self) -> str:
        """将消息转换为JSON字符串"""
        return json.dumps(self.model_dump(), ensure_ascii=False)
    
    def model_dump(self) -> Dict[str, Any]:
        """自定义序列化方法，对外仍输出 name/tool_call_id 以兼容 API 与存储。"""
        message = {"role": self.role.value}
        if self.content is not None:
            message["content"] = self.content
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.model_dump() for tool_call in self.tool_calls]
        if self.name is not None and self.tool_call_id is not None:
            message["name"] = self.name
            message["tool_call_id"] = self.tool_call_id
            message["success"] = self.success
            if self.tool_params is not None:
                message["tool_params"] = self.tool_params
        if self.metadata is not None:
            message["metadata"] = self.metadata
        if self.create_time:
            message["create_time"] = self.create_time.strftime("%Y-%m-%d %H:%M:%S")
        return message

    def to_context(self) -> Dict[str, Any]:
        """提供给 LLM API 的消息格式：仅 role/content/name/tool_call_id，不包含 metadata。"""
        message = {"role": self.role.value}
        if self.content is not None:
            message["content"] = self.content
        if self.tool_calls is not None:
            formatted=[]
            for tc in self.tool_calls:
                filtered={k:v for k,v in tc.function.arguments.items() if not k.startswith("__args_")}
                arguments=json.dumps(filtered,ensure_ascii=False)
                formatted.append({"id": tc.id,"type": tc.type,"function": {"name": tc.function.name,"arguments": arguments}})
            message["tool_calls"]=formatted
        if self.name is not None and self.tool_call_id is not None:
            message["name"] = self.name
            message["tool_call_id"] = self.tool_call_id
        return message    

    def to_user_message(self) -> Dict[str, Any]:
        """用户可读格式；工具调用/工具结果的 content 为 JSON 字符串。"""
        if self.is_tool_result:
            content = self._tool_result_to_user_content()
        elif self.is_assistant_tool_calls:
            content = self._tool_calls_to_user_content()
        else:
            content = self.content or ""
        return {
            "role": self.role.value,
            "content": content,
            "create_time": (self.create_time or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _tool_calls_to_user_content(self) -> str:
        """工具调用：content 为 JSON，供前端解析展示。每个 tool 条目带 tool_call_id 供 UI 匹配结果。"""
        tools = [
            {
                "tool_call_id": tc.id,
                "tool_name": tc.function.name,
                "tool_params": dict(tc.function.arguments or {}),
            }
            for tc in (self.tool_calls or [])
        ]
        content = ""
        if self.content and self.content.strip():
            content = self.content.strip()

        payload: Dict[str, Any] = {
            "kind": "tool_call",
            "tools": tools,
            "text": content,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _tool_result_to_user_content(self) -> str:
        """工具结果：content 为 JSON，供前端解析展示。"""
        raw = _strip_ansi((self.content or "").strip())
        lines = []
        try:
            obj = json.loads(raw)
            lines.append("```json")
            lines.append(json.dumps(obj, ensure_ascii=False, indent=2))
            lines.append("```")
        except (json.JSONDecodeError, TypeError):
            if _looks_like_code(raw):
                lines.append("```")
                lines.append(raw)
                lines.append("```")
            else:
                lines.append("```text")
                lines.append(raw or "(无)")
                lines.append("```")
        if self.metadata and self.metadata.get("truncated") and self.metadata.get("outputPath"):
            lines.append("")
            lines.append(f"*（输出已截断，完整内容已保存至：`{self.metadata['outputPath']}`）*")
        content = "\n".join(lines)

        payload: Dict[str, Any] = {
            "kind": "tool_result",
            "tool_name": (self.name or "") + " Executed",
            "tool_call_id": self.tool_call_id,
            "success": self.success,
            "tool_params": self.tool_params or {},
            "tool_result": content,
        }
        return json.dumps(payload, ensure_ascii=False)
