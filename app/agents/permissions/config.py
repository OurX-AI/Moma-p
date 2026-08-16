"""从 agent_config.permissions 加载配置。"""
from __future__ import annotations
from dataclasses import dataclass, field
from .rule import PermissionRule
from .schemes import BUILTIN_MODES, ModeDefinition, UnmatchedPolicy
from .validator import PermissionConfigValidator


@dataclass
class PermissionConfig:
    """调用级权限配置（与 tools.permissions 工具装配配置分离）。"""

    default_mode: str = "default"
    modes: dict[str, ModeDefinition] = field(default_factory=dict)
    allow: list[PermissionRule] = field(default_factory=list)
    deny: list[PermissionRule] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_agent_config(cls, agent_config: dict | None) -> PermissionConfig:
        raw = agent_config.get("permissions") if isinstance(agent_config, dict) else None
        if not isinstance(raw, dict):
            # 无配置时不拦业务规则，只靠硬拒绝熔断
            return cls.disabled_passthrough()
        return cls.from_dict(raw)

    @classmethod
    def disabled_passthrough(cls) -> PermissionConfig:
        return cls(
            default_mode="default",
            modes=dict(BUILTIN_MODES),
            allow=[],
            deny=[],
            enabled=False,
        )

    @classmethod
    def from_dict(cls, raw: dict) -> PermissionConfig:
        modes = dict(BUILTIN_MODES)
        modes_raw = raw.get("modes")
        if isinstance(modes_raw, dict):
            cleaned = PermissionConfigValidator.validate_modes(modes_raw)
            for key, body in cleaned.items():
                unmatched = UnmatchedPolicy(body["unmatched"])
                modes[key] = ModeDefinition(
                    name=key,
                    unmatched=unmatched,
                    description=str(body.get("description") or ""),
                )

        allow: list[PermissionRule] = []
        for item in PermissionConfigValidator.validate_rule_list(
            list(raw.get("allow") or []), field="allow"
        ):
            rule = PermissionRule.parse(item)
            if rule:
                allow.append(rule)

        deny: list[PermissionRule] = []
        for item in PermissionConfigValidator.validate_rule_list(
            list(raw.get("deny") or []), field="deny"
        ):
            rule = PermissionRule.parse(item)
            if rule:
                deny.append(rule)

        default_mode = str(raw.get("defaultMode") or "default").strip() or "default"
        if default_mode not in modes:
            modes[default_mode] = ModeDefinition(
                name=default_mode,
                unmatched=UnmatchedPolicy.ALLOW,
                description="auto-created unknown mode, unmatched=allow",
            )

        enabled = raw.get("enabled")
        if enabled is None:
            enabled = True

        return cls(
            default_mode=default_mode,
            modes=modes,
            allow=allow,
            deny=deny,
            enabled=bool(enabled),
        )

    def resolve_mode(self) -> ModeDefinition:
        return self.modes.get(self.default_mode) or BUILTIN_MODES["default"]
