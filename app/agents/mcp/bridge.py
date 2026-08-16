import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING
from ..schemes import RuntimeContext
from ..tools.utils import await_with_abort
from ..tools.mcp.mcp_wrapper import MCPToolWrapper, mcp_registry_tool_name
from .pool import parse_service_id


if TYPE_CHECKING:
    from ..tools.factory import ToolsFactory

META_TOOL_NAMES = frozenset({
    "mcp_list_resources",
    "mcp_read_resource",
    "mcp_list_prompts",
    "mcp_get_prompt",
    "mcp_search_tools",
})


def mcp_bridge_from_run_ctx(run_ctx: RuntimeContext) -> Optional["MCPAgentBridge"]:
    bridge = run_ctx.mcp_bridge
    return bridge if isinstance(bridge, MCPAgentBridge) else None


@dataclass
class MCPServerRunState:
    server_id: str
    session: Any
    timeout_sec: float
    tool_defs: List[Any]
    lazy: bool
    resources_enabled: bool = True
    prompts_enabled: bool = False
    registered: Set[str] = field(default_factory=set)
    cfg: Optional[Dict[str, Any]] = None


def _format_resource_contents(contents: Any) -> str:
    parts: List[str] = []
    for item in contents or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
            continue
        blob = getattr(item, "blob", None)
        if blob:
            parts.append(f"[binary resource content, {len(blob)} bytes]")
    return "\n".join(parts) if parts else "(empty resource)"


def _format_content_block(content: Any) -> str:
    text = getattr(content, "text", None)
    if text is not None:
        return str(text)
    data = getattr(content, "data", None)
    mime = getattr(content, "mimeType", None) or getattr(content, "mime_type", None) or ""
    if data is not None:
        size = len(data) if hasattr(data, "__len__") else "?"
        return f"[{mime or 'content'} block, {size} bytes]"
    return str(content)


def _format_prompt_messages(messages: Any) -> str:
    lines: List[str] = []
    for msg in messages or []:
        role = getattr(msg, "role", None) or "unknown"
        content = getattr(msg, "content", None)
        body = _format_content_block(content)
        lines.append(f"### {role}\n{body}")
    return "\n\n".join(lines) if lines else "(empty prompt)"


class MCPAgentBridge:
    """单次 Agent run 的 MCP 会话桥：Resource 访问、延迟加载工具注册。"""

    def __init__(self, factory: "ToolsFactory") -> None:
        self._factory = factory
        self.servers: Dict[str, MCPServerRunState] = {}  # key: server_id, value: MCPServerRunState
        self._meta_registered = False
        self._pinned_server_ids: Set[str] = set()  # 已 pin 的 MCP server ids

    def add_server(
        self,
        *,
        server_id: str,
        session: Any,
        timeout_sec: float,
        tool_defs: List[Any],
        lazy: bool,
        resources_enabled: bool = True,
        prompts_enabled: bool = False,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.servers[server_id] = MCPServerRunState(
            server_id=server_id,
            session=session,
            timeout_sec=timeout_sec,
            tool_defs=list(tool_defs or []),
            lazy=lazy,
            resources_enabled=resources_enabled,
            prompts_enabled=prompts_enabled,
            cfg=cfg,
        )

    def add_server_deferred(
        self,
        cfg: Dict[str, Any],
        *,
        lazy: bool = True,
        resources_enabled: bool = True,
        prompts_enabled: bool = False,
    ) -> None:
        """lazy 模式：仅登记配置，首次使用时再 get_or_connect。"""
        server_id = parse_service_id(cfg)
        timeout_ms = cfg.get("timeout_ms") or 30000
        self.servers[server_id] = MCPServerRunState(
            server_id=server_id,
            session=None,
            timeout_sec=timeout_ms / 1000.0,
            tool_defs=[],
            lazy=lazy,
            resources_enabled=resources_enabled,
            prompts_enabled=prompts_enabled,
            cfg=dict(cfg),
        )

    def connected_server_ids(self) -> List[str]:
        '''
        获取已建连的 MCP server ids
        '''
        return [sid for sid, state in self.servers.items() if state.session is not None]

    async def pin_and_touch_connected(self) -> None:
        """已建连的 MCP 在 run 期间 pin，并刷新 idle 计时。"""
        from .pool import MCP_POOL
        connected_ids = self.connected_server_ids()
        unpinned_ids = [sid for sid in connected_ids if sid not in self._pinned_server_ids]   # 获取未 pin 的 MCP server ids
        if unpinned_ids:
            await MCP_POOL.pin_by_server_ids(unpinned_ids)
            self._pinned_server_ids.update(unpinned_ids)   # 更新已 pin 的 MCP server ids
        if connected_ids:   # pin 后刷新 last_used，长 run 期间降低误清理概率（pin 仍为主保护）。
            await MCP_POOL.touch_by_server_ids(connected_ids)

    async def unpin_all(self) -> None:
        '''
        解除所有 pin，允许 idle cleanup 回收。
        '''
        if not self._pinned_server_ids:
            return
        
        from .pool import MCP_POOL
        await MCP_POOL.unpin_by_server_ids(list(self._pinned_server_ids))
        self._pinned_server_ids.clear()

    async def ensure_server_connected(
        self,
        server_id: str,
        run_ctx: Optional[RuntimeContext] = None,
    ) -> bool:
        '''
        确保 MCP server 已连接
        '''
        if run_ctx is not None and run_ctx.is_aborted():
            return False
        server_state = self.servers.get(server_id)
        if server_state is None:
            return False
        
        # 如果 MCP server 已连接，则返回 True
        if server_state.session is not None:
            return True
        
        # 如果 MCP server 未配置，则返回 False
        if not server_state.cfg:
            return False
        
        # 获取 MCP server 连接
        from .pool import MCP_POOL
        result = await await_with_abort(run_ctx, MCP_POOL.get_or_connect(server_state.cfg))
        if result is None:
            return False
        session, _sid, timeout_sec, tool_defs = result
        server_state.session = session
        server_state.timeout_sec = timeout_sec
        if not server_state.tool_defs:
            server_state.tool_defs = list(tool_defs or [])
        await self.pin_and_touch_connected()
        return True

    async def _ensure_targets_connected(
        self,
        targets: List[str],
        run_ctx: Optional[RuntimeContext] = None,
    ) -> bool:
        ok = True
        for sid in targets:
            if run_ctx is not None and run_ctx.is_aborted():
                return False
            if not await self.ensure_server_connected(sid, run_ctx):
                if run_ctx is not None and run_ctx.is_aborted():
                    return False
                ok = False
        return ok

    def _server_ids(
        self,
        server_id: Optional[str],
        *,
        resources: bool = False,
        prompts: bool = False,
    ) -> List[str]:
        '''
        获取需要操作的 MCP server ids
        '''
        if server_id:
            key = server_id.strip()
            if key not in self.servers:
                return []
            server_state = self.servers[key]
            if resources and not server_state.resources_enabled:
                return []
            if prompts and not server_state.prompts_enabled:
                return []
            return [key]
        ids: List[str] = []
        for sid, server_state in self.servers.items():
            if resources and not server_state.resources_enabled:
                continue
            if prompts and not server_state.prompts_enabled:
                continue
            ids.append(sid)
        return ids

    def register_meta_tools_once(self) -> None:
        '''
        注册一次元工具
        '''
        if self._meta_registered or not self.servers:
            return
        from ..tools.mcp.mcp_prompt import MCPGetPromptTool, MCPListPromptsTool
        from ..tools.mcp.mcp_resource import MCPListResourcesTool, MCPReadResourceTool
        from ..tools.mcp.mcp_search import MCPSearchToolsTool

        if any(s.resources_enabled for s in self.servers.values()):
            if not self._factory.has_tool("mcp_list_resources"):
                self._factory.register_tool(MCPListResourcesTool())
            if not self._factory.has_tool("mcp_read_resource"):
                self._factory.register_tool(MCPReadResourceTool())
        if any(s.prompts_enabled for s in self.servers.values()):
            if not self._factory.has_tool("mcp_list_prompts"):
                self._factory.register_tool(MCPListPromptsTool())
            if not self._factory.has_tool("mcp_get_prompt"):
                self._factory.register_tool(MCPGetPromptTool())
        if not self._factory.has_tool("mcp_search_tools"):
            self._factory.register_tool(MCPSearchToolsTool())
        self._meta_registered = True

    def _register_tool_def(self, server_id: str, tool_def: Any) -> Optional[str]:
        '''
        注册工具定义
        '''
        server_state = self.servers.get(server_id)
        if server_state is None:
            return None
        original_name = getattr(tool_def, "name", None)
        if not original_name:
            return None
        registry_name = mcp_registry_tool_name(server_id, original_name)
        if self._factory.has_tool(registry_name):
            server_state.registered.add(registry_name)
            return registry_name
        if registry_name in server_state.registered:
            return None
        wrapper = MCPToolWrapper(
            server_state.session,
            server_id,
            tool_def,
            timeout_seconds=server_state.timeout_sec,
        )
        self._factory.register_tool(wrapper)
        server_state.registered.add(registry_name)
        return registry_name

    def register_all_tools(self, server_id: str) -> int:
        server_state = self.servers.get(server_id)
        if server_state is None:
            return 0
        count = 0
        for tool_def in server_state.tool_defs:
            if self._register_tool_def(server_id, tool_def):
                count += 1
        return count

    async def list_resources(
        self,
        server_id: Optional[str] = None,
        run_ctx: Optional[RuntimeContext] = None,
    ) -> Optional[str]:
        if run_ctx is not None and run_ctx.is_aborted():
            return None
        
        targets = self._server_ids(server_id, resources=True)
        if not targets:
            return "No MCP servers available for listing resources."

        if not await self._ensure_targets_connected(targets, run_ctx):
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            return "No MCP servers available for listing resources."

        lines: List[str] = []
        for sid in targets:
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            server_state = self.servers[sid]
            try:
                resources: List[Any] = []
                cursor: Optional[str] = None
                while True:
                    if run_ctx is not None and run_ctx.is_aborted():
                        return None
                    result = await await_with_abort(
                        run_ctx,
                        server_state.session.list_resources(cursor),
                    )
                    if result is None:
                        if run_ctx is not None and run_ctx.is_aborted():
                            return None
                        raise RuntimeError("list_resources: no result")
                    batch = getattr(result, "resources", []) or []
                    resources.extend(batch)
                    cursor = getattr(result, "nextCursor", None)
                    if not cursor:
                        break
            except Exception as e:
                lines.append(f"[{sid}] list_resources failed: {e}")
                continue
            lines.append(f"## server: {sid} ({len(resources)} resources)")
            if not resources:
                lines.append("(none)")
                continue
            for res in resources:
                uri = getattr(res, "uri", "") or ""
                name = getattr(res, "name", "") or ""
                desc = getattr(res, "description", "") or ""
                mime = getattr(res, "mimeType", "") or ""
                meta = f" — {desc}" if desc else ""
                mime_part = f" [{mime}]" if mime else ""
                label = name or str(uri)
                lines.append(f"  - {label}{mime_part}{meta}")
                lines.append(f"    uri: {uri}")
        return "\n".join(lines)

    async def read_resource(
        self,
        uri: str,
        server_id: Optional[str] = None,
        run_ctx: Optional[RuntimeContext] = None,
    ) -> Optional[str]:
        if run_ctx is not None and run_ctx.is_aborted():
            return None
        if not uri or not uri.strip():
            return "uri is required"
        uri_text = uri.strip()
        targets = self._server_ids(server_id, resources=True)
        if not targets:
            return "No MCP servers available for reading resources."

        if not await self._ensure_targets_connected(targets, run_ctx):
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            return "No MCP servers available for reading resources."

        errors: List[str] = []
        for sid in targets:
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            server_state = self.servers[sid]
            try:
                result = await await_with_abort(run_ctx, server_state.session.read_resource(uri_text))
                if result is None:
                    if run_ctx is not None and run_ctx.is_aborted():
                        return None
                    errors.append(f"[{sid}] read_resource: no result")
                    continue
            except Exception as e:
                errors.append(f"[{sid}] {e}")
                continue
            body = _format_resource_contents(getattr(result, "contents", None))
            return f"<server>{sid}</server>\n<uri>{uri_text}</uri>\n<content>\n{body}\n</content>"
        return "Failed to read resource:\n" + "\n".join(errors)

    async def list_prompts(
        self,
        server_id: Optional[str] = None,
        run_ctx: Optional[RuntimeContext] = None,
    ) -> Optional[str]:
        if run_ctx is not None and run_ctx.is_aborted():
            return None
        targets = self._server_ids(server_id, prompts=True)
        if not targets:
            return "No MCP servers available for listing prompts."

        if not await self._ensure_targets_connected(targets, run_ctx):
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            return "No MCP servers available for listing prompts."

        lines: List[str] = []
        for sid in targets:
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            server_state = self.servers[sid]
            try:
                prompts: List[Any] = []
                cursor: Optional[str] = None
                while True:
                    if run_ctx is not None and run_ctx.is_aborted():
                        return None
                    result = await await_with_abort(
                        run_ctx,
                        server_state.session.list_prompts(cursor),
                    )
                    if result is None:
                        if run_ctx is not None and run_ctx.is_aborted():
                            return None
                        raise RuntimeError("list_prompts: no result")
                    batch = getattr(result, "prompts", []) or []
                    prompts.extend(batch)
                    cursor = getattr(result, "nextCursor", None)
                    if not cursor:
                        break
            except Exception as e:
                lines.append(f"[{sid}] list_prompts failed: {e}")
                continue
            lines.append(f"## server: {sid} ({len(prompts)} prompts)")
            if not prompts:
                lines.append("(none)")
                continue
            for prompt in prompts:
                name = getattr(prompt, "name", "") or ""
                title = getattr(prompt, "title", "") or ""
                desc = getattr(prompt, "description", "") or ""
                label = title or name
                meta = f" — {desc}" if desc else ""
                lines.append(f"  - {label}{meta}")
                lines.append(f"    name: {name}")
                args = getattr(prompt, "arguments", None) or []
                for arg in args:
                    arg_name = getattr(arg, "name", "") or ""
                    arg_desc = getattr(arg, "description", "") or ""
                    required = getattr(arg, "required", False)
                    req = " (required)" if required else ""
                    arg_meta = f": {arg_desc}" if arg_desc else ""
                    lines.append(f"    arg {arg_name}{req}{arg_meta}")
        return "\n".join(lines)

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        server_id: Optional[str] = None,
        run_ctx: Optional[RuntimeContext] = None,
    ) -> Optional[str]:
        if run_ctx is not None and run_ctx.is_aborted():
            return None
        if not name or not name.strip():
            return "name is required"
        prompt_name = name.strip()
        arg_map: Optional[Dict[str, str]] = None
        if arguments:
            arg_map = {str(k): str(v) for k, v in arguments.items()}
        targets = self._server_ids(server_id, prompts=True)
        if not targets:
            return "No MCP servers available for getting prompts."

        if not await self._ensure_targets_connected(targets, run_ctx):
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            return "No MCP servers available for getting prompts."

        errors: List[str] = []
        for sid in targets:
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            server_state = self.servers[sid]
            try:
                result = await await_with_abort(
                    run_ctx,
                    server_state.session.get_prompt(prompt_name, arg_map),
                )
                if result is None:
                    if run_ctx is not None and run_ctx.is_aborted():
                        return None
                    errors.append(f"[{sid}] get_prompt: no result")
                    continue
            except Exception as e:
                errors.append(f"[{sid}] {e}")
                continue
            desc = getattr(result, "description", None) or ""
            body = _format_prompt_messages(getattr(result, "messages", None))
            header = f"<server>{sid}</server>\n<prompt>{prompt_name}</prompt>"
            if desc:
                header += f"\n<description>{desc}</description>"
            return f"{header}\n<messages>\n{body}\n</messages>"
        return "Failed to get prompt:\n" + "\n".join(errors)

    async def search_tools(
        self,
        query: Optional[str],
        server_id: Optional[str],
        activate: Optional[List[str]],
        run_ctx: Optional[RuntimeContext] = None,
    ) -> Optional[str]:
        if run_ctx is not None and run_ctx.is_aborted():
            return None
        targets = self._server_ids(server_id)
        if not targets:
            return "No MCP servers available."

        if not await self._ensure_targets_connected(targets, run_ctx):
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            return "No MCP servers available."

        q = (query or "").strip().lower()
        q_re = re.compile(re.escape(q), re.IGNORECASE) if q else None

        pending_rows: List[tuple[str, str, str]] = []
        loaded: List[str] = []

        for sid in targets:
            if run_ctx is not None and run_ctx.is_aborted():
                return None
            server_state = self.servers[sid]
            for tool_def in server_state.tool_defs:
                original = getattr(tool_def, "name", "") or ""
                registry_name = mcp_registry_tool_name(sid, original)
                desc = getattr(tool_def, "description", "") or ""
                if registry_name in server_state.registered or self._factory.has_tool(registry_name):
                    loaded.append(f"{registry_name}: {desc}".strip())
                    continue
                if q_re and not (q_re.search(registry_name) or q_re.search(original) or q_re.search(desc)):
                    continue
                pending_rows.append((sid, registry_name, desc))

        activated: List[str] = []
        if activate:
            wanted = {str(n).strip() for n in activate if str(n).strip()}
            for sid, registry_name, _ in pending_rows:
                if run_ctx is not None and run_ctx.is_aborted():
                    return None
                if registry_name not in wanted:
                    continue
                state = self.servers[sid]
                tool_def = next(
                    (t for t in state.tool_defs if mcp_registry_tool_name(sid, getattr(t, "name", "")) == registry_name),
                    None,
                )
                if tool_def is not None and self._register_tool_def(sid, tool_def):
                    activated.append(registry_name)
            missing = wanted - set(activated) - set(loaded)
            for name in sorted(missing):
                if not self._factory.has_tool(name):
                    logging.warning("mcp_search_tools: unknown or unavailable tool %r", name)

        lines = [f"Pending MCP tools ({len(pending_rows)}):"]
        if pending_rows:
            for _, registry_name, desc in pending_rows[:100]:
                short = (desc[:120] + "...") if len(desc) > 120 else desc
                lines.append(f"  - {registry_name}: {short}")
            if len(pending_rows) > 100:
                lines.append(f"  ... ({len(pending_rows) - 100} more)")
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append(f"Loaded MCP tools ({len(loaded)}):")
        if loaded:
            lines.extend(f"  - {row}" for row in loaded[:100])
        else:
            lines.append("  (none)")

        if activate is not None:
            lines.append("")
            if activated:
                lines.append("Activated this call: " + ", ".join(activated))
            else:
                lines.append("Activated this call: (none)")
            lines.append("Loaded tools are callable directly in subsequent turns.")

        return "\n".join(lines)
