"""工具权限策略：将 config permissions / toolset / mode / spawn 参数解析为工具名列表。"""
from __future__ import annotations
import logging
from typing import Any, Dict, FrozenSet, List, Optional, Set
from .catalog import (
    catalog_tool_is_readonly,
    catalog_tool_names,
    catalog_toolsets,
    tool_names_for_toolset,
)


DELEGATION_TOOL_NAME = "spawn"
SPAWN_STATUS_TOOL_NAME = "spawn_status"
SUBAGENT_TOOLSET = "subagent"

SUBAGENT_HARD_DENIED_TOOLS: FrozenSet[str] = frozenset(
    {
        DELEGATION_TOOL_NAME,
        SPAWN_STATUS_TOOL_NAME,
        "ask_question",
        "cron",
        "todo_read",
        "todo_write",
    }
)

class ToolPolicyResolver:
    """统一解析主 Agent / 子 Agent 可用工具名（不含 MCP，MCP 仍按 server 白名单注册）。"""

    @staticmethod
    def _expand_tools_from_toolsets(toolsets: List[str]) -> Set[str]:
        """将 toolset 名列表展开为对应的内建工具名集合。"""
        names: Set[str] = set()
        for ts in toolsets:
            key = (ts or "").strip()
            if not key:
                continue
            names.update(tool_names_for_toolset(key))
        return names

    @staticmethod
    def _decision_is_allow(decision: Any) -> bool:
        """配置项是否为 allow。"""
        return str(decision or "").strip().lower() == "allow"

    @staticmethod
    def _decision_is_deny(decision: Any) -> bool:
        """配置项是否为 deny。"""
        return str(decision or "").strip().lower() == "deny"

    @staticmethod
    def _get_permissions_maps(permissions: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """从 permissions 取出并规范化 toolsets / tools 两层映射。"""
        toolsets_map = permissions.get("toolsets")
        tools_map = permissions.get("tools")
        if not isinstance(toolsets_map, dict):
            toolsets_map = {}
        if not isinstance(tools_map, dict):
            tools_map = {}
        return toolsets_map, tools_map

    @classmethod
    def _resolve_toolsets_map(
        cls,
        toolsets_map: Dict[str, Any],
        *,
        all_toolsets: Set[str],
    ) -> tuple[Set[str], Set[str]]:
        """解析 toolsets 层的 allow/deny，返回应加入白名单与应排除的工具名集合。"""
        allowed: Set[str] = set()
        denied: Set[str] = set()
        for key, decision in toolsets_map.items():
            name = str(key).strip()
            if not name:
                continue
            if name not in all_toolsets:
                logging.warning("tool policy: unknown toolset %r, skipped", name)
                continue
            if cls._decision_is_allow(decision):
                allowed.update(tool_names_for_toolset(name))
            elif cls._decision_is_deny(decision):
                denied.update(tool_names_for_toolset(name))
        return allowed, denied

    @classmethod
    def _resolve_tools_map(
        cls,
        tools_map: Dict[str, Any],
        *,
        all_tools: Set[str],
    ) -> tuple[Set[str], Set[str]]:
        """解析 tools 层的 allow/deny，返回应加入白名单与应排除的单工具名集合。"""
        allowed: Set[str] = set()
        denied: Set[str] = set()
        for key, decision in tools_map.items():
            name = str(key).strip()
            if not name:
                continue
            if name not in all_tools:
                logging.warning("tool policy: unknown tool %r, skipped", name)
                continue
            if cls._decision_is_allow(decision):
                allowed.add(name)
            elif cls._decision_is_deny(decision):
                denied.add(name)
        return allowed, denied

    @classmethod
    def resolve_agent_tools(
        cls,
        agent_config: Dict[str, Any],
    ) -> List[str]:
        """解析主 Agent 内建工具白名单（不含 MCP）。"""
        tools_block = agent_config.get("tools") if isinstance(agent_config.get("tools"), dict) else {}
        permissions = tools_block.get("permissions") if isinstance(tools_block.get("permissions"), dict) else {}
        toolsets_map, tools_map = cls._get_permissions_maps(permissions)
        all_toolsets = set(catalog_toolsets())
        all_tools = set(catalog_tool_names())
        ts_allow, ts_deny = cls._resolve_toolsets_map(toolsets_map, all_toolsets=all_toolsets)
        tool_allow, tool_deny = cls._resolve_tools_map(tools_map, all_tools=all_tools)
        return sorted((ts_allow | tool_allow) - ts_deny - tool_deny)

    @classmethod
    def resolve_spawn_tools(
        cls,
        *,
        parent_tool_names: List[str],
        subagent_config: Dict[str, Any],
    ) -> List[str]:
        """子 Agent 工具集解析。

        1. 父当前工具 − 硬排除
        2. 若子配置声明了 tools.permissions，再与其子白名单求交
        3. 最后若 ``readonly``（缺省 True）为真，再丢掉非只读工具
           （防止子 permissions 误放行写工具后仍被只读闸住）
        """
        tools_names = {str(n) for n in parent_tool_names if n}
        tools_names -= SUBAGENT_HARD_DENIED_TOOLS

        tools_block = subagent_config.get("tools") if isinstance(subagent_config, dict) else None
        tools_block = tools_block if isinstance(tools_block, dict) else {}
        permissions = tools_block.get("permissions") if isinstance(tools_block.get("permissions"), dict) else {}
        toolsets_map, tools_map = cls._get_permissions_maps(permissions)
        if toolsets_map or tools_map:
            tools_names &= set(cls.resolve_agent_tools(subagent_config))

        readonly = True
        if isinstance(subagent_config, dict) and "readonly" in subagent_config:
            readonly = bool(subagent_config.get("readonly"))
        if readonly:
            tools_names = {n for n in tools_names if catalog_tool_is_readonly(n) is True}

        return sorted(tools_names)
