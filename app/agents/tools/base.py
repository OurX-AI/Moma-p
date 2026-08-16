from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from ..schemes import AgentContext, RuntimeContext
from .schemes import ToolResult


class BaseTool(ABC):
    """工具基类"""

    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    def __init__(self, agent_ctx: Optional[AgentContext] = None) -> None:
        self._agent_ctx = agent_ctx

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""
        pass

    @abstractmethod
    def description(self, params: Optional[Dict[str, Any]] = None) -> str:
        """给 UI 的短描述（工具列表、进度展示等）。

        params 为本次调用入参，便于后续按调用生成展示文案；当前多数工具可忽略。
        """
        pass

    def prompt(self) -> str:
        """给 Agent 的完整工具说明（写入 to_param / LLM schema）。

        默认回退 description()。仅说明会随会话变化的工具（如 spawn）需要覆盖。
        """
        return self.description()

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """工具参数定义

        Returns:
            Dict[str, Dict[str, str]]: {
                "param_name": {
                    "type": "参数类型",
                    "description": "参数描述"
                }
            }
        """
        pass

    @abstractmethod
    def is_readonly(self, params: Optional[Dict[str, Any]] = None) -> bool:
        """是否只读。params 为空时为工具级结论；有入参时可按本次调用细判。"""
        pass

    @abstractmethod
    def is_parallel(self, params: Optional[Dict[str, Any]] = None) -> bool:
        """是否可并行。params 为空时为工具级结论；有入参时可按本次调用细判。"""
        pass

    def result_truncate_spec(self):
        """Factory 统一截断时的按工具覆盖；默认 None 表示用内置表/全局默认。"""
        return None

    def is_available(self) -> bool:
        """当前运行时是否应向模型暴露本工具；默认 True。"""
        return True

    @abstractmethod
    async def execute(self, agent_ctx: AgentContext, run_ctx: RuntimeContext, **kwargs: Any) -> ToolResult:
        """执行工具调用

        Args:
            agent_ctx: 代理上下文
            runtime_ctx: 运行时上下文
            kwargs: 工具参数

        Returns:
            ToolResult: 工具执行结果
        """
        pass

    def to_param(self) -> Dict:
        """Convert tool to function call format；Agent 侧说明走 prompt()。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.prompt(),
                "parameters": self.parameters,
            },
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return self._validate(params, {**schema, "type": "object"}, "")

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        t, label = schema.get("type"), path or "parameter"
        if t in self._TYPE_MAP and not isinstance(val, self._TYPE_MAP[t]):
            return [
                f"[TYPE_MISMATCH] {label} should be {t}, got {type(val).__name__}. "
                f"Fix the argument type and retry."
            ]

        errors = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(
                f"[INVALID_ENUM] {label} must be one of {schema['enum']}, got {val!r}. "
                f"Pick an allowed value and retry."
            )
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(
                    f"[OUT_OF_RANGE] {label} must be >= {schema['minimum']}, got {val}. "
                    f"Increase the value and retry."
                )
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(
                    f"[OUT_OF_RANGE] {label} must be <= {schema['maximum']}, got {val}. "
                    f"Decrease the value and retry."
                )
        if t == "string":
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(
                    f"[INVALID_VALUE] {label} must be at least {schema['minLength']} chars. "
                    f"Provide a longer value and retry."
                )
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(
                    f"[INVALID_VALUE] {label} must be at most {schema['maxLength']} chars. "
                    f"Shorten the value and retry."
                )
        if t == "object":
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    field = path + "." + k if path else k
                    errors.append(
                        f"[MISSING_REQUIRED] missing required parameter `{field}`. "
                        f"Include it in the tool arguments and retry."
                    )
            for k, v in val.items():
                if k in props:
                    errors.extend(self._validate(v, props[k], path + "." + k if path else k))
        if t == "array" and "items" in schema:
            for i, item in enumerate(val):
                errors.extend(self._validate(item, schema["items"], f"{path}[{i}]" if path else f"[{i}]"))
        return errors
