"""MCP 连接注册：按 agent 配置将 MCP 工具注册到 ToolsFactory。"""
import logging
from typing import Any, Dict, List, Optional
from ..tools.factory import ToolsFactory
from .bridge import MCPAgentBridge
from .pool import MCP_POOL, parse_mcp_utilities, parse_service_id


class MCPServerConnector:
    """根据配置从连接池获取或创建 MCP 连接，并将工具注册到 factory。"""

    @staticmethod
    def resolve_lazy(cfg: Dict[str, Any]) -> bool:
        """未写 lazy 时默认 True（defer）；显式 lazy=false 则全量注入。"""
        if "lazy" in cfg:
            return bool(cfg.get("lazy"))
        return True

    @staticmethod
    async def connect_and_register(
        servers: List[Dict[str, Any]],
        factory: ToolsFactory,
    ) -> Optional[MCPAgentBridge]:
        bridge = MCPAgentBridge(factory)
        for cfg in servers:
            server_id = parse_service_id(cfg)
            lazy = MCPServerConnector.resolve_lazy(cfg)
            resources_enabled, prompts_enabled = parse_mcp_utilities(cfg)
            if lazy:
                bridge.add_server_deferred(
                    cfg,
                    lazy=True,
                    resources_enabled=resources_enabled,
                    prompts_enabled=prompts_enabled,
                )
                logging.info(
                    "MCP server %s: lazy deferred, connect on first use",
                    server_id,
                )
            else:
                result = await MCP_POOL.get_or_connect(cfg)
                if result is None:
                    continue
                session, server_id, timeout_sec, tool_defs = result
                bridge.add_server(
                    server_id=server_id,
                    session=session,
                    timeout_sec=timeout_sec,
                    tool_defs=tool_defs,
                    lazy=False,
                    resources_enabled=resources_enabled,
                    prompts_enabled=prompts_enabled,
                )
                registered = bridge.register_all_tools(server_id)
                logging.info("MCP server %s: session ready, %d tools registered", server_id, registered)

        if not bridge.servers:
            return None

        bridge.register_meta_tools_once()
        return bridge
