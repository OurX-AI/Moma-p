"""权限模式与裁决结果类型。"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class UnmatchedPolicy(str, Enum):
    """未命中 allow/deny 时的兜底策略。"""

    ALLOW = "allow"
    DENY = "deny"
    READONLY_OR_DENY = "readonly_or_deny"


@dataclass(frozen=True)
class ModeDefinition:
    """单个 permission mode 的定义。"""

    name: str
    unmatched: UnmatchedPolicy
    description: str = ""


# 内置模式：配置可覆盖同名项
BUILTIN_MODES: dict[str, ModeDefinition] = {
    "default": ModeDefinition(
        name="default",
        unmatched=UnmatchedPolicy.ALLOW,
        description="未匹配规则时放行（极端熔断除外）",
    ),
    "strict": ModeDefinition(
        name="strict",
        unmatched=UnmatchedPolicy.DENY,
        description="未匹配一律拒绝",
    ),
    "readonly": ModeDefinition(
        name="readonly",
        unmatched=UnmatchedPolicy.READONLY_OR_DENY,
        description="未匹配时仅内置只读命令放行，其余拒绝",
    ),
}
