"""MCP 连接池：按参数复用连接，idle 回收与 run 期间 pin。"""
import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client


IDLE_TIMEOUT_SEC = 300
CLEANUP_INTERVAL_SEC = 60


def parse_service_id(cfg: Dict[str, Any]) -> str:
    """从 MCP server 配置解析稳定 server_id（id 优先，其次 name）。"""
    default = "mcp"
    for key in ("id", "name"):
        value = cfg.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def parse_mcp_utilities(cfg: Dict[str, Any]) -> Tuple[bool, bool]:
    """解析 MCP server 是否启用 Resource / Prompt 元工具（默认 resources=True、prompts=False）。"""
    resources_enabled = True
    prompts_enabled = False
    if isinstance(cfg.get("resources"), bool):
        resources_enabled = cfg["resources"]
    if isinstance(cfg.get("prompts"), bool):
        prompts_enabled = cfg["prompts"]
    utilities = cfg.get("utilities")
    if isinstance(utilities, dict):
        if isinstance(utilities.get("resources"), bool):
            resources_enabled = utilities["resources"]
        if isinstance(utilities.get("prompts"), bool):
            prompts_enabled = utilities["prompts"]
    tools_block = cfg.get("tools")
    if isinstance(tools_block, dict):
        if isinstance(tools_block.get("resources"), bool):
            resources_enabled = tools_block["resources"]
        if isinstance(tools_block.get("prompts"), bool):
            prompts_enabled = tools_block["prompts"]
    return resources_enabled, prompts_enabled


def _build_mcp_request_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    """组装远程 MCP 请求头；支持 headers 字典、api_key_env、auth=bearer_env|bearer。"""
    headers: Dict[str, str] = {}

    # 解析 headers 字典
    raw_headers = cfg.get("headers")
    if isinstance(raw_headers, dict):
        for key, value in raw_headers.items():
            if key and value is not None:
                headers[str(key)] = str(value)
    
    # 解析 service_id
    service_id = parse_service_id(cfg)
    # 解析 auth 字段
    auth_raw = cfg.get("auth")
    auth = str(auth_raw).strip().lower() if auth_raw is not None else ""
    api_key_env = cfg.get("api_key_env")
    if auth in ("", "none"):
        if api_key_env and os.environ.get(api_key_env):
            headers["Authorization"] = f"Bearer {os.environ.get(api_key_env)}"
        return headers
    if auth in ("bearer", "bearer_env", "bearer-env"):
        env_name = cfg.get("token_env") or api_key_env
        token = os.environ.get(env_name) if env_name else None
        if not token and cfg.get("token"):
            token = str(cfg.get("token"))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers
    if auth == "oauth":
        logging.warning(
            "MCP server %s: auth=oauth is not implemented yet; use bearer_env or static headers",
            service_id,
        )
        return headers
    logging.warning("MCP server %s: unknown auth %r, using headers only", service_id, auth_raw)
    return headers


class MCPPool:
    """进程级 MCP 连接池，按连接参数指纹缓存；后台定时关闭空闲超时的连接。"""

    def __init__(self, idle_timeout_sec: float = IDLE_TIMEOUT_SEC) -> None:
        self._lock = asyncio.Lock()
        self._server_id_to_key: Dict[str, str] = {}   # key: server_id, value: connection_key
        self._key_locks: Dict[str, asyncio.Lock] = {}   # key: connection_key, value: 连接锁
        self._sessions: Dict[str, List[Any]] = {} # key: connection_key, value: [stack, session, server_id, timeout_sec, tools, last_used]
        self._pin_counts: Dict[str, int] = {}    # key: connection_key, value: 连接引用次数，为0才可超时回收
        self._idle_timeout_sec = idle_timeout_sec   # 空闲超时时间
        self._cleanup_task: Optional[asyncio.Task] = None   # 空闲清理任务

    def _key_lock(self, key: str) -> asyncio.Lock:
        if key not in self._key_locks:
            self._key_locks[key] = asyncio.Lock()
        return self._key_locks[key]

    @staticmethod
    def _fingerprint_segment(items: frozenset[tuple[str, str]]) -> str:
        """空集合不拼段，避免 key 末尾多余 ':' 或 'frozenset()' 字面量。"""
        if not items:
            return ""
        return str(items)

    def _connection_key(self, cfg: Dict[str, Any]) -> str:
        """相同 command/endpoint 的配置复用同一连接，与 agent_type/server_id 无关。"""
        server_type = (cfg.get("type") or "stdio").lower()
        command = cfg.get("command") or ""
        args = tuple(cfg.get("args") or [])
        endpoint = cfg.get("endpoint") or cfg.get("url") or ""
        env = cfg.get("env") or {}
        env_fp = self._fingerprint_segment(frozenset((k, str(v)) for k, v in env.items()))
        header_fp = self._fingerprint_segment(
            frozenset((k, str(v)) for k, v in _build_mcp_request_headers(cfg).items())
        )
        parts = [server_type, command, str(args), endpoint]
        if env_fp:
            parts.append(env_fp)
        if header_fp:
            parts.append(header_fp)
        return ":".join(parts)

    async def get_or_connect(self, cfg: Dict[str, Any]) -> Optional[Tuple[Any, str, float, List[Any]]]:
        """
        获取或创建连接。返回 (session, server_id, timeout_sec, tool_defs)。
        有缓存则更新 last_used 并返回；无缓存则建连并入库。空闲释放由后台任务负责。
        """
        server_id = parse_service_id(cfg)
        timeout_ms = cfg.get("timeout_ms") or 30000
        timeout_sec = timeout_ms / 1000.0
        enabled_tools = cfg.get("tools")
        if enabled_tools is not None and not isinstance(enabled_tools, list):
            enabled_tools = []
        key = self._connection_key(cfg)
        now = asyncio.get_running_loop().time()

        async with self._lock:
            if key in self._sessions:
                entry = self._sessions[key]
                _stack, session, _server_id, _timeout_sec, tools = entry[0], entry[1], entry[2], entry[3], entry[4]
                entry[5] = now
                self._server_id_to_key[server_id] = key
                return (session, server_id, timeout_sec, self._enabled_tools(tools, enabled_tools))
            key_lock = self._key_lock(key)

        async with key_lock:
            async with self._lock:
                if key in self._sessions:
                    entry = self._sessions[key]
                    entry[5] = now
                    _stack, session, _server_id, _timeout_sec, tools = entry[0], entry[1], entry[2], entry[3], entry[4]
                    self._server_id_to_key[server_id] = key
                    return (session, server_id, timeout_sec, self._enabled_tools(tools, enabled_tools))
            try:
                stack, session, tools = await self._new_connection(cfg, timeout_sec)
            except Exception as e:
                logging.error("MCP pool connect %s failed: %s", server_id, e)
                return None
            async with self._lock:
                self._sessions[key] = [stack, session, server_id, timeout_sec, tools, now]
                self._server_id_to_key[server_id] = key
        return (session, server_id, timeout_sec, self._enabled_tools(tools, enabled_tools))

    def _enabled_tools(self, tools: List[Any], enabled_names: Optional[List[str]]) -> List[Any]:
        """enabled_names 为 None 时注册全部；空列表不注册；否则按名称过滤。"""
        if enabled_names is None:
            return tools
        if not enabled_names:
            return []
        enabled = set(str(n) for n in enabled_names if n)
        return [t for t in tools if getattr(t, "name", None) in enabled]

    async def _new_connection(self, cfg: Dict[str, Any], timeout_sec: float) -> Tuple[AsyncExitStack, Any, List[Any]]:
        server_type = (cfg.get("type") or "stdio").lower()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if server_type == "stdio":
                command = cfg.get("command")
                args = cfg.get("args") or []
                if not command:
                    raise ValueError("missing command")
                env = cfg.get("env") or {}
                params = StdioServerParameters(command=command, args=args, env=env or None)
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            elif server_type in ("http", "streamable_http", "streamable-http"):
                url = cfg.get("url") or cfg.get("endpoint")
                if not url:
                    raise ValueError("missing url or endpoint")
                headers = _build_mcp_request_headers(cfg)
                streams = await stack.enter_async_context(
                    streamablehttp_client(
                        url,
                        headers=headers or None,
                        timeout=timeout_sec,
                        sse_read_timeout=timeout_sec,
                    )
                )
                read_stream, write_stream = streams[0], streams[1]
            elif server_type == "sse":
                endpoint = cfg.get("endpoint") or cfg.get("url")
                if not endpoint:
                    raise ValueError("missing endpoint or url")
                headers = _build_mcp_request_headers(cfg)
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(endpoint, headers=headers or None, timeout=timeout_sec)
                )
            else:
                raise ValueError(f"unknown server type {server_type}")
            mcp_session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await mcp_session.initialize()
            list_result = await mcp_session.list_tools()
            tools = getattr(list_result, "tools", []) or []
            return (stack, mcp_session, tools)
        except Exception:
            await stack.aclose()
            raise

    async def pin_by_server_ids(self, server_ids: List[str]) -> None:
        """Agent run 借用连接期间禁止 idle cleanup 回收。"""
        if not server_ids:
            return
        async with self._lock:
            for server_id in server_ids:
                key = self._server_id_to_key.get(server_id)
                if not key:
                    continue
                self._pin_counts[key] = self._pin_counts.get(key, 0) + 1

    async def unpin_by_server_ids(self, server_ids: List[str]) -> None:
        '''
        解除 pin，允许 idle cleanup 回收。
        '''
        if not server_ids:
            return
        async with self._lock:
            for server_id in server_ids:
                key = self._server_id_to_key.get(server_id)
                if not key or key not in self._pin_counts:
                    continue
                self._pin_counts[key] -= 1
                if self._pin_counts[key] <= 0:
                    self._pin_counts.pop(key, None)

    async def touch_by_server_ids(self, server_ids: List[str]) -> None:
        '''
        刷新 last_used，长 run 期间降低误清理概率（pin 仍为主保护）。
        '''
        if not server_ids:
            return
        now = asyncio.get_running_loop().time()
        async with self._lock:
            for server_id in server_ids:
                key = self._server_id_to_key.get(server_id)
                if not key or key not in self._sessions:
                    continue
                self._sessions[key][5] = now

    async def _cleanup_idle(self) -> None:
        """关闭空闲超过 _idle_timeout_sec 的连接（在持锁外 aclose，避免阻塞）。"""
        now = asyncio.get_running_loop().time()
        to_close: List[Tuple[str, AsyncExitStack]] = []
        async with self._lock:
            for key, entry in list(self._sessions.items()):
                # 如果连接被 pin，则不关闭
                if self._pin_counts.get(key, 0) > 0:
                    continue
                if now - entry[5] > self._idle_timeout_sec:
                    to_close.append((key, entry[0]))
                    del self._sessions[key]
            if to_close:
                dead_ids = [sid for sid, k in list(self._server_id_to_key.items()) if k in {t[0] for t in to_close}]
                for sid in dead_ids:
                    self._server_id_to_key.pop(sid, None)
        for key, stack in to_close:
            try:
                await stack.aclose()
                logging.debug("MCP pool closed idle connection: %s", key[:80])
            except Exception as e:
                logging.warning("MCP pool close idle failed %s: %s", key[:80], e)

    def start_idle_cleanup(self) -> None:
        """启动后台任务，定期清理空闲连接。应在应用 startup 时调用。"""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL_SEC)
                await self._cleanup_idle()

        self._cleanup_task = asyncio.create_task(_loop())
        logging.info("MCP pool idle cleanup started, interval=%ss, idle_timeout=%ss", CLEANUP_INTERVAL_SEC, self._idle_timeout_sec)

    def stop_idle_cleanup(self) -> None:
        """停止后台清理任务。应在应用 shutdown 时调用。"""
        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        self._cleanup_task = None


MCP_POOL = MCPPool()
