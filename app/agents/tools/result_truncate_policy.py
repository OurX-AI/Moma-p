"""按工具覆盖的结果截断策略：全局 Truncate 默认 + 单工具 max 行/字节/方向。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional
from app.config.settings import settings

if TYPE_CHECKING:
    from .base import BaseTool


@dataclass(frozen=True)
class ToolResultTruncateSpec:
    """单工具截断覆盖；字段为 None 表示沿用全局 settings。"""

    max_lines: Optional[int] = None
    max_bytes: Optional[int] = None
    direction: Literal["head", "tail"] = "head"


class ToolResultTruncatePolicy:
    """解析有效截断参数：工具实例覆盖 > 按名内置表 > 全局默认。"""

    # 内置覆盖：shell 留尾（失败日志在末尾）；搜/拉网页更紧；read 按行留头
    _BUILTIN: dict[str, ToolResultTruncateSpec] = {
        "bash": ToolResultTruncateSpec(
            max_bytes=10000,
            direction="tail",
        ),
        "powershell": ToolResultTruncateSpec(
            max_bytes=10000,
            direction="tail",
        ),
        "shell_process": ToolResultTruncateSpec(
            max_bytes=10000,
            direction="tail",
        ),
        "grep_search": ToolResultTruncateSpec(
            max_lines=400,
            max_bytes=30000,
            direction="head",
        ),
        "glob_search": ToolResultTruncateSpec(
            max_lines=500,
            max_bytes=30000,
            direction="head",
        ),
        "web_fetch": ToolResultTruncateSpec(
            max_lines=800,
            max_bytes=30000,
            direction="head",
        ),
        "web_search": ToolResultTruncateSpec(
            max_lines=200,
            max_bytes=20000,
            direction="head",
        ),
        "browser": ToolResultTruncateSpec(
            max_lines=400,
            max_bytes=30000,
            direction="head",
        ),
        "lsp": ToolResultTruncateSpec(
            max_lines=400,
            max_bytes=30000,
            direction="head",
        ),
        "read_file": ToolResultTruncateSpec(
            direction="head",
        ),
    }

    @classmethod
    def resolve(cls, tool_name: str, tool: Optional["BaseTool"] = None) -> ToolResultTruncateSpec:
        if tool is not None:
            override = tool.result_truncate_spec()
            if override is not None:
                return override
        return cls._BUILTIN.get(tool_name) or ToolResultTruncateSpec()

    @classmethod
    def effective_limits(cls, spec: ToolResultTruncateSpec) -> tuple[int, int, Literal["head", "tail"]]:
        max_lines = (
            settings.tool_result_truncate_max_lines
            if spec.max_lines is None
            else int(spec.max_lines)
        )
        max_bytes = (
            settings.tool_result_truncate_max_bytes
            if spec.max_bytes is None
            else int(spec.max_bytes)
        )
        return max(1, max_lines), max(1, max_bytes), spec.direction
