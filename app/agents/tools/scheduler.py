import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from ..schemes import RuntimeContext
from .factory import ToolsFactory
from .schemes import ToolCallItem, ToolErrorResult, ToolResult


_ADVANCE_POLL_SEC = 0.05


@dataclass
class ToolRunNotifier:
    """单轮工具调度回调：工具开始执行与结果产出。"""

    on_tool_run_result: Callable[[ToolCallItem, ToolResult], Awaitable[None]]
    on_tool_run_started: Optional[Callable[[ToolCallItem], Awaitable[None]]] = None


class ToolScheduleSession:
    """单轮流式工具 run 调度器：队列、分组驱动执行、notifier 回调。分组规则由 ToolsFactory 提供。"""

    def __init__(
        self,
        factory: ToolsFactory,
        notifier: ToolRunNotifier,
    ) -> None:
        """绑定 Factory、notifier，初始化入队列表、分组与在途任务状态。"""
        self._factory = factory
        self._notifier = notifier
        self._items: List[ToolCallItem] = []
        self._groups: List[List[ToolCallItem]] = []
        self._results: Dict[int, ToolResult] = {}
        self._item_tasks: Dict[int, asyncio.Task[None]] = {}
        self._stream_finalized = False
        self._lock = asyncio.Lock()

    def submit(self, run_ctx: RuntimeContext, item: ToolCallItem) -> None:
        """入队一条工具调用，并异步触发调度。"""
        if self._stream_finalized:
            raise RuntimeError("tool run already finalized; cannot submit")
        if run_ctx.is_aborted():
            return
        
        # 工具加入队里，并刷新并行执行分组
        self._items.append(item)
        if not self._groups:
            self._groups = self._factory.partition_parallel_groups(self._items)
        else:
            self._groups = self._factory.refresh_parallel_groups(self._groups, [item])
        
        # 启动异步调度
        asyncio.create_task(self._advance(run_ctx))

    def _previous_group_done(self, group_index: int, ranges: List[Tuple[int, int]]) -> bool:
        """组间串行：仅判断紧邻前一组是否均已产出结果。"""
        if group_index == 0:
            return True
        start, end = ranges[group_index - 1]
        return all(idx in self._results for idx in range(start, end))

    async def _advance(self, run_ctx: RuntimeContext) -> None:
        """推进调度：前一组全部完成后，对当前组内尚未执行的 tool 逐条启动。"""
        async with self._lock:
            if run_ctx.is_aborted():
                return
            if not self._items:
                return

            # 获取每个分组对应的工具列表范围
            groups = self._groups
            ranges: List[Tuple[int, int]] = []
            index = 0
            for group in groups:
                ranges.append((index, index + len(group)))
                index += len(group)

            # 按照组调度工具
            for group_index, (start, end) in enumerate(ranges):
                # 检查前一个群组任务是否结束
                if not self._previous_group_done(group_index, ranges):
                    return
                for idx in range(start, end):
                    if idx in self._results or idx in self._item_tasks:
                        continue
                    self._item_tasks[idx] = asyncio.create_task(self._run_item(run_ctx, idx))
                    self._item_tasks[idx].add_done_callback(
                        lambda _t, rc=run_ctx, i=idx: asyncio.create_task(self._on_item_done(rc, i))
                    )

    async def _run_item(self, run_ctx: RuntimeContext, index: int) -> None:
        """执行单条工具；处理取消与异常。"""
        tool = self._items[index]
        try:
            await self._notify_start(tool)
            result = await self._factory.execute(run_ctx, tool)
            self._results[index] = result
            await self._notify_result(tool, result)
        except asyncio.CancelledError:
            if index not in self._results:
                result = run_ctx.aborted_tool_result(tool.tool_name or "unknown")
                self._results[index] = result
                await self._notify_result(tool, result)
            raise
        except Exception as e:
            logging.error("tool run item %s failed: %s", tool.tool_name, e)
            result = ToolErrorResult(str(e))
            self._results[index] = result
            await self._notify_result(tool, result)

    async def _on_item_done(self, run_ctx: RuntimeContext, index: int) -> None:
        """单条任务完成：移除条目任务记录并再次调用 _advance。"""
        self._item_tasks.pop(index, None)
        await self._advance(run_ctx)

    async def _notify_start(self, item: ToolCallItem) -> None:
        """触发 on_tool_run_started；未配置或异常时仅记录日志。"""
        handler = self._notifier.on_tool_run_started
        if handler is None:
            return
        try:
            await handler(item)
        except Exception as e:
            logging.error("on_tool_run_started failed for %s: %s", item.tool_name, e)

    async def _notify_result(self, item: ToolCallItem, result: ToolResult) -> None:
        """触发 on_tool_run_result；异常时仅记录日志，不中断调度。"""
        try:
            await self._notifier.on_tool_run_result(item, result)
        except Exception as e:
            logging.error("on_tool_run_result failed for %s: %s", item.tool_name, e)

    async def wait_complete(self, run_ctx: RuntimeContext) -> None:
        """标记 LLM 流已结束，补调度未启动条目并等待全部完成。"""
        self._stream_finalized = True
        await self._advance(run_ctx)
        while not run_ctx.is_aborted() and not self._all_resolved():
            await self._advance(run_ctx)
            await asyncio.sleep(_ADVANCE_POLL_SEC)
        if run_ctx.is_aborted():
            await self.cancel(run_ctx)
            return
        await self._advance(run_ctx)
        await self._await_inflight_tasks()

    def discard_tasks(self) -> None:
        """取消在途任务；不触发 notifier（Agent reset / 中止兜底）。"""
        self._stream_finalized = True
        for task in list(self._item_tasks.values()):
            if not task.done():
                task.cancel()
        self._item_tasks.clear()
        self._groups.clear()

    async def cancel(self, run_ctx: RuntimeContext) -> None:
        """取消在途任务，未完成项补 aborted 并回调。"""
        for task in list(self._item_tasks.values()):
            if not task.done():
                task.cancel()
        await self._await_inflight_tasks()
        for idx, item in enumerate(self._items):
            if idx in self._results:
                continue
            result = run_ctx.aborted_tool_result(item.tool_name or "unknown")
            self._results[idx] = result
            await self._notify_result(item, result)

    def _all_resolved(self) -> bool:
        if not self._items:
            return True
        return all(idx in self._results for idx in range(len(self._items)))

    async def _await_inflight_tasks(self) -> None:
        """等待当前在途的单条任务全部结束。"""
        pending = list(self._item_tasks.values())
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
