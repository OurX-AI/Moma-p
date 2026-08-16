import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
import json_repair
from pydantic import BaseModel, field_validator


class ToolArgsParser:
    ARGS_ERROR_KEY="__args_error__"

    @staticmethod
    def parse(v: Any) -> Dict[str, Any]:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and not v.strip():
            return {}
        if not isinstance(v, str):
            return {
                ToolArgsParser.ARGS_ERROR_KEY:"args is invalid.",
            }

        s1=v.strip()
        s2=ToolArgsParser._strip_code_fence(s1)  # 去除代码块
        s=ToolArgsParser._strip_outer_quotes(s2)  # 去除外层引号

        out=ToolArgsParser._try_parse_json_and_repair(s)
        if out is not None:
            return dict(out)

        # 直接解析失败，进一步处理
        truncated_guess=ToolArgsParser._looks_truncated(s)
        if truncated_guess:
            return {
                ToolArgsParser.ARGS_ERROR_KEY:"args is truncated by llm.",
            }

        return {
            ToolArgsParser.ARGS_ERROR_KEY:"args is parsed failed.",
        }

    
    @staticmethod
    def _strip_code_fence(s: str) -> str:
        # 去除代码块
        if not s.startswith("```"):
            return s
        lines=[ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        return "\n".join(lines).strip()

    @staticmethod
    def _strip_outer_quotes(s: str) -> str:
        # 去除外层引号
        if len(s)<2:
            return s
        if s[0]==s[-1] and s[0] in ("'",'"'):
            return s[1:-1].strip()
        return s

    @staticmethod
    def _try_parse_json_and_repair(s: str) -> Optional[Dict[str, Any]]:
        try:
            o=json.loads(s)
            if isinstance(o, dict):
                return o
            if isinstance(o, str):
                try:
                    o2=json.loads(o)
                    if isinstance(o2, dict):
                        return o2
                except Exception:
                    pass
        except Exception:
            pass

        try:
            o=json_repair.loads(s)
            if isinstance(o, dict):
                return o
            if isinstance(o, str):
                try:
                    o2=json_repair.loads(o)
                    if isinstance(o2, dict):
                        return o2
                except Exception:
                    pass
        except Exception:
            pass

        return None

    @staticmethod
    def _looks_truncated(s: str) -> bool:
        if "{" not in s: # 如果s中不包含大括号，则认为没有被截断了
            return False
        if not s.rstrip().endswith("}"): # 如果最后一个字符不是大括号，则认为被截断了
            return True
        return ToolArgsParser._final_brace_depth(s)>0 # 如果最后一个大括号的深度大于0，则认为被截断了

    @staticmethod
    def _final_brace_depth(text: str) -> int:
        # 计算最后一个大括号的深度
        i=0
        n=len(text)
        depth=0
        in_str=False
        esc=False
        while i<n:
            ch=text[i]
            if in_str:
                if esc:
                    esc=False
                elif ch=="\\":
                    esc=True
                elif ch=='"':
                    in_str=False
            else:
                if ch=='"':
                    in_str=True
                elif ch=="{":
                    depth+=1
                elif ch=="}":
                    depth-=1
                    if depth<0:
                        depth=0
            i+=1
        return depth


class TokenUsage(BaseModel):
    input_tokens:int=0
    output_tokens:int=0
    cache_read_tokens:int=0
    cache_write_tokens:int=0
    total_tokens:int=0
    reasoning_tokens:int=0
    tool_tokens:int=0
    other_tokens:int=0

    def overflow_basis(self)->int:
        tokens=(self.input_tokens or 0)+(self.cache_read_tokens or 0)+(self.cache_write_tokens or 0)
        if tokens>0:
            return tokens
        return self.total_tokens or 0

    def add(self,other:"TokenUsage")->"TokenUsage":
        if other is None:
            return self
        self.input_tokens+=(other.input_tokens or 0)
        self.output_tokens+=(other.output_tokens or 0)
        self.cache_read_tokens+=(other.cache_read_tokens or 0)
        self.cache_write_tokens+=(other.cache_write_tokens or 0)
        self.total_tokens+=(other.total_tokens or 0)
        self.reasoning_tokens+=(other.reasoning_tokens or 0)
        self.tool_tokens+=(other.tool_tokens or 0)
        self.other_tokens+=(other.other_tokens or 0)
        return self


class ModelLimits(BaseModel):
    context_limit:Optional[int]=None
    max_output_tokens:Optional[int]=None
    max_input_tokens:Optional[int]=None


class ChatResponse(BaseModel):   
    """聊天响应格式"""
    success: bool = True         # 返回申请情况下成功与否
    content: str                 # 返回内容，包含成功情况下正确内容和失败情况下错误信息


class ToolInfo(BaseModel):
    """工具调用信息"""
    id: str
    name: str
    args: Dict[str, Any]

    @field_validator("args", mode="before")
    @classmethod
    def _args_must_be_dict(cls, v: Any) -> Dict[str, Any]:
        return ToolArgsParser.parse(v)


class AskToolResponse(BaseModel):
    """工具调用响应格式"""
    success: bool = True         # 返回申请情况下成功与否
    content: Optional[str] = None                 # 返回内容，包含成功情况下思考内容和失败情况下错误信息
    tool_calls: Optional[List[ToolInfo]] = None  # 支持多个工具调用


class LLMInfo(BaseModel):
    """LLM信息模型"""
    name: str
    type: str
    description: str
    max_tokens: int
    api_style: str


@dataclass(frozen=True)
class StreamTextDelta:
    """流式文本增量。"""
    text: str

@dataclass(frozen=True)
class StreamToolCallReady:
    """单个 tool call 参数已闭合，可调度执行。"""
    id: str
    name: str
    arguments: Dict[str, Any]

@dataclass(frozen=True)
class StreamEnd:
    """流结束；usage 为 TokenUsage 或空。"""
    usage: Any

LLMStreamEvent = Union[StreamTextDelta, StreamToolCallReady, StreamEnd]

