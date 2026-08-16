import asyncio
import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional
from app.agents.permissions import PermissionEngine
from app.config.settings import settings
from ..schemes import AgentContext, RuntimeContext
from .base import BaseTool
from .catalog import (
    ensure_tools_catalog_loaded,
    get_tool_catalog,
)
from .exec.common import peel_cd_prefix, resolve_working_dir
from .policy import DELEGATION_TOOL_NAME
from .schemes import ToolCallItem, ToolErrorResult, ToolResult, ToolResultStatus, ToolSuccessResult
from .truncation import Truncate
from .result_truncate_policy import ToolResultTruncatePolicy


TOOLS_CACHE_NAME = ()
MAX_CACHE_SIZE = 256
_ABORT_POLL_SEC = 0.2
_PATH_ARG_KEY = "path"


class FileContentCache:
    """文件内容 LRU 缓存，避免重复读取相同文件。"""

    def __init__(self, max_size: int = 50, ttl_seconds: int = 300):
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds

    def get(self, key: str) -> Optional[str]:
        """获取缓存内容，如果过期则返回 None。"""
        if key not in self._cache:
            return None
        content, ts = self._cache[key]
        if time.time() - ts > self._ttl_seconds:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return content

    def put(self, key: str, content: str) -> None:
        """存储内容到缓存。"""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (content, time.time())
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)


# 全局文件内容缓存实例
FILE_CONTENT_CACHE = FileContentCache()


def _cache_key(tool_name: str, tool_params: Dict[str, Any]) -> tuple[str, str]:
    """工具名 + 参数生成可哈希的缓存键（参数按 key 排序序列化）。"""
    return (tool_name, json.dumps(tool_params, sort_keys=True))


def _extract_scope_path(args: dict) -> Optional[str]:
    """从工具参数提取 path，规范化后用于同批次并行冲突比较。"""
    raw = args.get(_PATH_ARG_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw.strip()).expanduser().as_posix()


def _paths_overlap(left: str, right: str) -> bool:
    """判断两个路径是否指向同一文件或存在父子目录重叠。"""
    left_parts = Path(left).parts
    right_parts = Path(right).parts
    if not left_parts or not right_parts:
        return bool(left_parts) == bool(right_parts) and bool(left_parts)
    common_len = min(len(left_parts), len(right_parts))
    return left_parts[:common_len] == right_parts[:common_len]


class ToolsFactory:
    """当前 Agent 的工具箱：装配实例、提供给 LLM 的 schema、统一 execute。"""

    def __init__(self, ctx: AgentContext) -> None:
        self._agent_ctx = ctx
        self._workspace_path = ctx.workspace_path
        self._tools: Dict[str, BaseTool] = {}
        self._cacheable: set[str] = set(TOOLS_CACHE_NAME)
        self._max_cache_size = MAX_CACHE_SIZE
        self._result_cache: Dict[tuple[str, str], ToolResult] = {}

    @classmethod
    def from_permissions(
        cls,
        *,
        allowed_names: List[str],
        ctx: AgentContext,
    ) -> "ToolsFactory":
        ensure_tools_catalog_loaded()
        factory = cls(ctx)
        for name in allowed_names:
            catalog = get_tool_catalog(name)
            if catalog is None:
                logging.warning("tool not in catalog, skip: %s", name)
                continue
            tool = catalog.instant(agent_ctx=ctx)
            if not tool.is_available():
                logging.info("tool unavailable on this host, skip: %s", name)
                continue
            factory.register_tool(tool)
        return factory

    def get_tool(self, name: str) -> BaseTool:
        return self._tools.get(name)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def list_tool_names(self) -> List[str]:
        return list(self._tools.keys())

    @property
    def has_spawn_tool(self) -> bool:
        """当前是否可委托子 Agent：由工具集合是否含 spawn 决定。"""
        return self.has_tool(DELEGATION_TOOL_NAME)

    def register_tool(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def register_tools(self, *tools: BaseTool) -> None:
        for tool in tools:
            self.register_tool(tool)

    def unregister_tool(self, name: str) -> None:
        self._tools.pop(name)

    def to_params(self) -> List[Dict[str, Any]]:
        """组装 LLM tools schema；各工具 Agent 说明走 prompt()。"""
        return [tool.to_param() for tool in self._tools.values()]

    def is_readonly(self, tool_name: str, params: dict | None = None) -> bool:
        tool = self.get_tool(tool_name)
        if tool is None:
            return False
        return bool(tool.is_readonly(params))

    def is_parallel(self, tool_name: str, params: dict | None = None) -> bool:
        tool = self.get_tool(tool_name)
        if tool is None:
            return False
        return bool(tool.is_parallel(params))

    def _can_parallelize_batch(self, tools: List[ToolCallItem]) -> bool:
        """判断一组工具调用是否可安全并发执行。"""
        if len(tools) <= 1:
            return False

        reserved_paths: List[str] = []

        for item in tools:
            tool_name = item.tool_name
            args = item.tool_params or {}
            if not isinstance(args, dict):
                logging.debug("Non-dict args for %s — defaulting to sequential", tool_name)
                return False

            if not self.is_parallel(tool_name, args):
                return False

            scoped_path = _extract_scope_path(args)
            if scoped_path is not None:
                if any(_paths_overlap(scoped_path, existing) for existing in reserved_paths):
                    return False
                reserved_paths.append(scoped_path)

        return True

    def partition_parallel_groups(self, tools: List[ToolCallItem]) -> List[List[ToolCallItem]]:
        """按 LLM 返回顺序做保序贪心拆分，得到可并行/需串行的连续分组。"""
        if not tools:
            return []

        groups: List[List[ToolCallItem]] = []
        current: List[ToolCallItem] = []

        for item in tools:
            candidate = current + [item]
            if len(candidate) == 1 or self._can_parallelize_batch(candidate):
                current = candidate
            else:
                groups.append(current)
                current = [item]

        if current:
            groups.append(current)

        return groups

    def refresh_parallel_groups(
        self,
        prior_groups: List[List[ToolCallItem]],
        new_items: List[ToolCallItem],
    ) -> List[List[ToolCallItem]]:
        """在已有分组上追加新工具：前面各组不变，仅刷新最后一组及之后。"""
        if not new_items:
            return prior_groups
        if not prior_groups:
            return self.partition_parallel_groups(new_items)

        # 仅对最后一个分组进行刷新，判断是否追加新工具
        groups = [list(group) for group in prior_groups[:-1]]
        tail = list(prior_groups[-1])
        for item in new_items:
            candidate = tail + [item]
            if len(candidate) == 1 or self._can_parallelize_batch(candidate):
                tail = candidate
            else:
                groups.append(tail)
                tail = [item]
        if tail:
            groups.append(tail)
        return groups

    async def execute_batch(
        self,
        run_ctx: RuntimeContext,
        tools: List[ToolCallItem],
    ) -> List[ToolResult]:
        """贪心拆分后按组调度执行，返回顺序与 calls 一致。"""
        if not tools:
            return []

        results: List[ToolResult] = []
        processed = 0
        for group in self.partition_parallel_groups(tools):
            if run_ctx.is_aborted():
                break
            
            if len(group) == 1:
                item = group[0]
                results.append(await self.execute(run_ctx, item))
            else:
                results.extend(await self.execute_parallel(run_ctx, group))
            processed += len(group)

        if run_ctx.is_aborted() and processed < len(tools):
            for item in tools[processed:]:
                results.append(run_ctx.aborted_tool_result(item.tool_name or "unknown"))
        return results

    async def execute_parallel(
        self,
        run_ctx: RuntimeContext,
        tools: List[ToolCallItem],
    ) -> List[ToolResult]:
        """并发执行一组已判定可并行的工具调用，返回顺序与 calls 一致。"""
        if not tools:
            return []

        workers = max(1, settings.max_parallel_tool_workers)
        semaphore = asyncio.Semaphore(workers)

        async def _run(item: ToolCallItem) -> ToolResult:
            async with semaphore:
                return await self.execute(run_ctx, item)

        tasks = [asyncio.create_task(_run(item)) for item in tools]
        try:
            while True:
                if run_ctx.is_aborted():
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    break
                if all(task.done() for task in tasks):
                    break
                await asyncio.sleep(0.2)
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[ToolResult] = []
        for item, outcome in zip(tools, outcomes):
            if isinstance(outcome, asyncio.CancelledError):
                results.append(run_ctx.aborted_tool_result(item.tool_name))
            elif isinstance(outcome, Exception):
                results.append(ToolErrorResult(str(outcome)))
            else:
                results.append(outcome)
        return results

    async def execute(self, run_ctx: RuntimeContext, tool: ToolCallItem) -> ToolResult:
        """执行工具调用"""
        tool_name = tool.tool_name
        try:
            # Agent任务中断，则直接返回工具执行终止
            if run_ctx.is_aborted():
                return run_ctx.aborted_tool_result(tool_name or "unknown")

            tool_params = tool.tool_params
            if not tool_name:
                return ToolErrorResult("Tool name is required")

            tool = self.get_tool(tool_name)
            if not tool:
                return ToolErrorResult(f"Tool {tool_name} not found")

            # 获取参数解析诊断字段
            args_error=tool_params.get("__args_error__") or None
            if args_error:
                msg=self._build_fix_hint(
                    tool_name=tool_name,
                    code="ARGS_PARSE_ERROR",
                    reason="the tool args from llm is invalid",
                    missing=[],
                    errors=[f"[ARGS_PARSE_ERROR] {args_error}. Fix JSON arguments and retry."],
                )
                return ToolErrorResult(msg)

            # 过滤解析诊断字段，避免影响真实工具执行
            clean_params={k:v for k,v in tool_params.items() if not k.startswith("__args_error__")}
            tool_params=clean_params

            # 检查参数是否有缺失
            required = set(tool.parameters.get("required", []) or [])
            provided = set(tool_params.keys())
            missing = required - provided
            if missing:
                msg = self._build_fix_hint(
                    tool_name=tool_name,
                    code="MISSING_REQUIRED",
                    reason="missing required parameters",
                    missing=sorted(list(missing)),
                    errors=[
                        f"[MISSING_REQUIRED] missing required parameter `{name}`. "
                        f"Include it in the tool arguments and retry."
                        for name in sorted(missing)
                    ],
                )
                return ToolErrorResult(msg)

            # 检查参数是否合法
            if hasattr(tool, "validate_params"):
                try:
                    errors = tool.validate_params(tool_params)  # type: ignore[attr-defined]
                except Exception as e:
                    msg = self._build_fix_hint(
                        tool_name=tool_name,
                        code="VALIDATION_FAILED",
                        reason="parameter validation raised an exception",
                        errors=[f"[VALIDATION_FAILED] {e}. Fix the arguments and retry."],
                    )
                    return ToolErrorResult(msg)
                if errors:
                    msg = self._build_fix_hint(
                        tool_name=tool_name,
                        code="INVALID_ARGUMENT",
                        reason="parameter validation failed",
                        errors=errors,
                    )
                    return ToolErrorResult(msg)

            # 调用级权限（全工具入口）; shell 先 peel cd, 与 CommandRunner 一致
            perm_params = tool_params
            if tool_name in ("bash", "powershell"):
                raw_cmd = str(tool_params.get("command") or "")
                cwd = resolve_working_dir(
                    tool_params.get("working_dir"),
                    self._agent_ctx.workspace_path,
                )
                workspace_root = (self._agent_ctx.workspace_path or "").strip() or None
                peeled_cmd, _ = peel_cd_prefix(raw_cmd, cwd, workspace_root)
                if peeled_cmd != raw_cmd:
                    perm_params = dict(tool_params)
                    perm_params["command"] = peeled_cmd

            permission = PermissionEngine.from_agent_config(self._agent_ctx.agent_config)
            decision = permission.decide_tool_call(tool_name, perm_params)
            if not decision.allowed:
                return ToolErrorResult(
                    f"Error: Tool blocked by permission policy ({decision.reason})"
                )

            # 可缓存工具：先查缓存
            if tool_name in self._cacheable:
                key = _cache_key(tool_name, tool_params)
                if key in self._result_cache:
                    logging.info("execute_tool: %s (cache hit)", tool_name)
                    return self._result_cache[key]

            # 执行工具调用（统一 abort 包装：长 await / 可 cancel 的 execute）
            result = await self._execute_with_abort(
                run_ctx,
                tool_name,
                tool.execute(self._agent_ctx, run_ctx, **tool_params),
            )

            # 仅对成功结果做超长截断，统一在 Factory 处理；支持按工具覆盖行/字节/方向
            if settings.enable_tool_result_truncate and result.status == ToolResultStatus.EXECUTE_SUCCESS:
                meta = result.metadata or {}
                if meta.get("truncated"):
                    # 工具已自行截断（如 shell 结构化输出），避免二次 head 截坏尾部
                    pass
                else:
                    spec = ToolResultTruncatePolicy.resolve(tool_name, tool)
                    max_lines, max_bytes, direction = ToolResultTruncatePolicy.effective_limits(spec)
                    raw = f"{result.result}"
                    truncated = Truncate.output(
                        raw,
                        max_lines=max_lines,
                        max_bytes=max_bytes,
                        direction=direction,
                        has_task_tool=self.has_spawn_tool,
                    )
                    if truncated.truncated and truncated.output_path:
                        result = ToolSuccessResult(
                            truncated.content,
                            metadata={
                                "truncated": True,
                                "outputPath": truncated.output_path,
                                "truncateDirection": direction,
                            },
                        )

            # 可缓存工具：写入缓存并限制容量
            if tool_name in self._cacheable:
                key = _cache_key(tool_name, tool_params)
                if self._max_cache_size and len(self._result_cache) >= self._max_cache_size:
                    oldest = next(iter(self._result_cache))
                    del self._result_cache[oldest]
                self._result_cache[key] = result

            return result

        except Exception as e:
            logging.error(f"Tool({tool_name}) execution error: {str(e)}")
            return ToolErrorResult(f"Tool execution error: {str(e)}")

    def _build_fix_hint(
        self,
        *,
        tool_name: str,
        reason: str,
        code: str = "INVALID_ARGUMENT",
        missing: List[str] = None,
        errors: List[str] = None,
    ) -> str:
        payload = {
            "error": "invalid_tool_arguments",
            "code": code,
            "tool": tool_name,
            "reason": reason,
            "missing": missing or [],
            "errors": errors or [],
            "guidance": [
                "Fix the tool arguments and call the tool again.",
                "Match required fields and types from the tool schema.",
                "Do not retry with the same invalid arguments.",
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _execute_with_abort(
        self,
        run_ctx: RuntimeContext,
        tool_name: str,
        coro,
    ) -> ToolResult:
        """统一包装 tool.execute：create_task + 轮询 abort。"""
        if run_ctx.is_aborted():
            return run_ctx.aborted_tool_result(tool_name)
        
        task = asyncio.create_task(coro)
        try:
            while not task.done():
                if run_ctx.is_aborted():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return run_ctx.aborted_tool_result(tool_name)
                await asyncio.sleep(_ABORT_POLL_SEC)
            return task.result()
        except asyncio.CancelledError:
            return run_ctx.aborted_tool_result(tool_name)
