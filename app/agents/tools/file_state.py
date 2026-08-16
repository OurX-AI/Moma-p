"""跨 Actor 文件读写协调：防止并发/SubAgent 场景下基于过期读视图写文件。"""
from __future__ import annotations
import asyncio
import os
import threading
import time
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


ReadStamp = Tuple[float, float, bool]
_MAX_PATHS_PER_ACTOR = 4096
_MAX_GLOBAL_WRITERS = 4096


class FileStateManager:
    """进程内单例：记录各 actor 的读 stamp、全局最后写入者，并提供 per-path 异步锁。"""

    def __init__(self) -> None:
        self._reads: Dict[str, Dict[str, ReadStamp]] = defaultdict(dict)  # 读记录，actor_id -> 路径 -> (mtime-文件最后修改时间, 读时刻, 是否片段读)
        self._last_writer: Dict[str, Tuple[str, float]] = {}  # 写记录，文件路径 -> (actor_id, 写入时刻)
        self._state_lock = threading.Lock()  # 全局状态锁，保护 _reads / _last_writer
        self._path_locks: Dict[str, asyncio.Lock] = {}  # 路径锁表，路径 -> asyncio.Lock
        self._meta_lock: Optional[asyncio.Lock] = None  # 路径锁元锁，懒创建，保护 _path_locks 条目并发创建

    @staticmethod
    def normalize_path(path: str | Path) -> str:
        """将路径规范化为绝对 posix 字符串，用作注册表 key。"""
        return Path(path).expanduser().resolve().as_posix()

    @staticmethod
    def _disabled() -> bool:
        """是否关闭 file_state（settings.ENABLE_FILE_STATE_GUARD）。"""
        from app.config.settings import settings
        return not settings.enable_file_state_guard

    @staticmethod
    def _trim_dict_over_limit(d: dict, limit: int) -> None:
        """超过 limit 时按插入顺序淘汰最旧条目，避免长会话无限增长。"""
        # 未超过限制，不需要裁剪
        over = len(d) - limit
        if over <= 0:
            return
        # 超过限制，按插入顺序淘汰最旧条目
        it = iter(d)
        for _ in range(over):
            try:
                d.pop(next(it))
            except (StopIteration, KeyError):
                break

    @staticmethod
    def _fmt_ts(ts: float) -> str:
        """将时间戳格式化为警告文案中的短时分秒。"""
        return time.strftime("%H:%M:%S", time.localtime(ts))

    @staticmethod
    def format_writes_since_reminder(writes: Dict[str, List[str]]) -> str:
        """将 get_write_records_since 结果格式化为 spawn 返回给父 Agent 的提醒段落。"""
        if not writes:
            return ""
        lines = [
            "While this subagent was running, other actors modified files the parent had previously read:",
        ]
        for actor_id, paths in sorted(writes.items()):
            for p in paths:
                lines.append(f"- {p} (by {actor_id})")
        lines.append("Re-read affected files before editing them.")
        return "\n".join(lines)

    def _meta(self) -> asyncio.Lock:
        """懒创建 meta 锁，用于保护 _path_locks 的并发创建。"""
        lock = self._meta_lock
        if lock is None:
            lock = asyncio.Lock()
            self._meta_lock = lock
        return lock

    async def _lock_for(self, resolved_path: str) -> asyncio.Lock:
        """获取（或创建）某路径对应的 asyncio.Lock。"""
        async with self._meta():
            path_lock = self._path_locks.get(resolved_path)
            if path_lock is None:
                path_lock = asyncio.Lock()
                self._path_locks[resolved_path] = path_lock
            return path_lock

    @asynccontextmanager
    async def get_path_lock(self, path: str | Path):
        """获取单路径锁后 yield 规范化路径，供 read→modify→write 临界区使用。"""
        resolved_path = self.normalize_path(path)
        if self._disabled():
            yield resolved_path
            return
        lock = await self._lock_for(resolved_path)
        async with lock:
            yield resolved_path

    @asynccontextmanager
    async def get_paths_lock(self, paths: Iterable[str | Path]):
        """按字典序获取多路径锁并同时持有后 yield 路径列表，避免多文件 patch 死锁。"""
        resolved_paths = sorted({self.normalize_path(p) for p in paths})
        if self._disabled() or not resolved_paths:
            yield resolved_paths
            return
        async with AsyncExitStack() as stack:
            for p in resolved_paths:
                await stack.enter_async_context(self.get_path_lock(p))
            yield resolved_paths

    def record_read(
        self,
        actor_id: str,
        path: str | Path,
        *,
        partial: bool = False,
        mtime: Optional[float] = None,
    ) -> None:
        """记录 actor 读过某文件；partial=True 表示片段读（offset/limit 或截断）。

        partial 仅作读视图元数据保留，不拦截后续 apply_patch/write（会话内至少读过一次即可编辑）。
        
        # 参数：   
        - actor_id: actor_id，标识Agent或SubAgent
        - path: 路径，文件路径
        - partial: 是否片段读
        - mtime: 文件最后修改时间，如果为None，则获取文件最后修改时间
        """
        if self._disabled() or not actor_id:
            return
        resolved_path = self.normalize_path(path)
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved_path)
            except OSError:
                return
        now = time.time()
        # 登记读记录，_reads[actor_id][resolved_path] = (mtime, 读时刻, partial)
        with self._state_lock:
            actor_reads = self._reads[actor_id]
            actor_reads[resolved_path] = (float(mtime), now, bool(partial))
            # 裁剪读记录，避免长会话无限增长
            self._trim_dict_over_limit(actor_reads, _MAX_PATHS_PER_ACTOR)

    def record_write(
        self,
        actor_id: str,
        path: str | Path,
        *,
        mtime: Optional[float] = None,
    ) -> None:
        """记录 actor 成功写入某文件，并更新全局 last_writer 与该 actor 的读 stamp。
        
        # 参数：   
        - actor_id: actor_id，标识Agent或SubAgent
        - path: 路径，文件路径
        - mtime: 文件最后修改时间，如果为None，则获取文件最后修改时间
        """
        if self._disabled() or not actor_id:
            return
        resolved_path = self.normalize_path(path)
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved_path)  # 文件最后修改时间
            except OSError:
                return
        now = time.time()
        # 登记写记录，_last_writer[resolved] = (actor_id, 写时刻)
        with self._state_lock:
            # 登记写记录，_last_writer[resolved_path] = (actor_id, 写时刻)
            self._last_writer[resolved_path] = (actor_id, now)
            # 裁剪写记录，避免长会话无限增长
            self._trim_dict_over_limit(self._last_writer, _MAX_GLOBAL_WRITERS)
            # 登记读记录，_reads[actor_id][resolved_path] = (mtime, 读时刻, False)
            self._reads[actor_id][resolved_path] = (float(mtime), now, False)
            # 裁剪读记录，避免长会话无限增长
            self._trim_dict_over_limit(self._reads[actor_id], _MAX_PATHS_PER_ACTOR)

    def check_stale_and_get_warning(self, actor_id: str, path: str | Path) -> Optional[str]:
        """写前检查视图是否过期，过期则返回 warning 文案，否则 None。
        
        # 参数：
        - actor_id: actor_id，标识Agent或SubAgent
        - path: 路径，文件路径
        """
        if self._disabled() or not actor_id:
            return None
        resolved_path = self.normalize_path(path)
        with self._state_lock:
            stamp = self._reads.get(actor_id, {}).get(resolved_path)
            last_writer = self._last_writer.get(resolved_path)

        # 如果读记录和写记录都为空，则视图未过期
        if stamp is None and last_writer is None:
            return None

        try:
            current_mtime = os.path.getmtime(resolved_path) # 文件最后修改时间
        except OSError:
            return None

        # 1. 其它 actor 在最后一次读之后写入
        if last_writer is not None:
            writer_id, writer_ts = last_writer
            if writer_id != actor_id:
                if stamp is None:
                    return (
                        f"{resolved_path} was modified by actor {writer_id!r} but this actor never read it. "
                        "Read the file before writing to avoid overwriting the other actor's changes."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved_path} was modified by actor {writer_id!r} at {self._fmt_ts(writer_ts)} — after "
                        f"this actor's last read at {self._fmt_ts(read_ts)}. Re-read the file before writing."
                    )

        # 2. 有读记录：检查磁盘 mtime 是否相对上次读已变化（片段读不告警）
        if stamp is not None:
            read_mtime, _read_ts, _partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"{resolved_path} was modified since you last read it on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing."
                )
            return None

        # 3. 无读记录但存在其它写入线索时，提示先读
        return (
            f"{resolved_path} was not read by this actor. "
            "Read the file first so you can write an informed edit."
        )

    def get_edit_block_reason(
        self,
        actor_id: str,
        path: str | Path,
        *,
        require_prior_read: bool,
    ) -> Optional[str]:
        """写前硬闸：未读/过期视图则返回错误文案，否则 None。

        会话内至少 read 过一次即可编辑；offset/limit 或截断的
        片段读不拦截。require_prior_read=True 用于改已有文件（update/overwrite/replace）。
        新建文件传 False，仅在有竞态写线索时仍可能拦截。
        """
        if self._disabled() or not actor_id:
            return None
        resolved_path = self.normalize_path(path)
        with self._state_lock:
            stamp = self._reads.get(actor_id, {}).get(resolved_path)
            last_writer = self._last_writer.get(resolved_path)

        if require_prior_read:
            if stamp is None:
                return (
                    f"Edit blocked: {resolved_path} was not read by this actor. "
                    "Call read_file first, then retry the edit."
                )

        try:
            current_mtime = os.path.getmtime(resolved_path)
        except OSError:
            return None

        if last_writer is not None:
            writer_id, writer_ts = last_writer
            if writer_id != actor_id:
                if stamp is None:
                    return (
                        f"Edit blocked: {resolved_path} was modified by actor {writer_id!r} "
                        "but this actor never read it. Read the file before writing."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"Edit blocked: {resolved_path} was modified by actor {writer_id!r} at "
                        f"{self._fmt_ts(writer_ts)} — after this actor's last read at "
                        f"{self._fmt_ts(read_ts)}. Re-read the file before writing."
                    )

        if stamp is not None:
            read_mtime, _read_ts, _partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"Edit blocked: {resolved_path} changed on disk since the last read. "
                    "Re-read the file before writing."
                )
        return None

    def get_write_records_since(
        self,
        exclude_actor_id: str,
        since_ts: float,
        paths: Iterable[str | Path],
    ) -> Dict[str, List[str]]:
        """查询 since_ts 之后、由其他 actor 写入的路径（供 spawn 结束提醒）。
        
        # 参数：   
        - exclude_actor_id: 排除的actor_id，标识Agent或SubAgent
        - since_ts: 时间戳，查询时间
        - paths: 路径列表，文件路径列表

        # 返回：   
        - 写记录，actor_id -> 路径列表
        """
        if self._disabled() or not exclude_actor_id:
            return {}
        
        # 规范化路径列表
        paths_set = {self.normalize_path(p) for p in paths}
        out: Dict[str, List[str]] = defaultdict(list)
        with self._state_lock:
            # 遍历写记录，_last_writer[resolved_path] = (actor_id, 写时刻)
            for p, (writer_id, ts) in self._last_writer.items():
                # 排除排除的actor_id
                if writer_id == exclude_actor_id:
                    continue
                # 排除写时刻早于 since_ts 的记录
                if ts < since_ts:
                    continue
                # 如果路径在 paths_set 中，则添加到结果中
                if p in paths_set:
                    # 添加到结果中，_last_writer[resolved_path] = (actor_id, 写时刻)
                    out[writer_id].append(p)
        return dict(out)

    def get_read_record_paths(self, actor_id: str) -> List[str]:
        """返回 actor 读记录中的规范化路径列表。"""
        if self._disabled() or not actor_id:
            return []
        with self._state_lock:
            return list(self._reads.get(actor_id, {}).keys())

    def append_warning(self, output: str, warning: Optional[str]) -> str:
        """将 stale 警告前置到工具成功输出中。"""
        if not warning:
            return output
        return "\n".join([
            "<warning>",
            warning,
            "</warning>",
            output,
        ])


FILE_STATE_MANAGER = FileStateManager()
