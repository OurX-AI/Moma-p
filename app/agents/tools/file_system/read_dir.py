import logging
from pathlib import Path
from typing import Any, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from .utils import ToolPathResolver, check_path_boundary


DEFAULT_READ_LIMIT = 2000


@register_tool(name="read_dir", toolset="filesystem")
class ReadDirTool(BaseTool):
    """Read directory entries with optional offset and limit."""

    @property
    def name(self) -> str:
        return "read_dir"

    def description(self, params=None) -> str:
        return f"""List entries in a directory.

When to use:
- Inspect immediate children of a known directory.
- Paginate large directories with `offset`/`limit` (default {DEFAULT_READ_LIMIT}).

When NOT to use:
- Recursive filename discovery (use `glob_search`).
- Content search (use `grep_search`).

Execution rules:
- Prefer absolute `path`; relative paths resolve against the agent workspace.
- Entries end with `/` for subdirectories.
- Parallelize independent directory reads.

Failure recovery:
- Path missing -> verify with `glob_search` or parent `read_dir`.
- Need deep tree map -> switch to `glob_search`."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative directory path to list.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional entry number to start from (1-indexed).",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Optional maximum number of entries to read. Default is {DEFAULT_READ_LIMIT}.",
                },
            },
            "required": ["path"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None
    ) -> ToolResult:
        try:
            if not path or not path.strip():
                return ToolErrorResult("Missing path parameter")

            if offset is not None and offset < 1:
                return ToolErrorResult("offset must be greater than or equal to 1")

            if limit is not None and limit < 1:
                return ToolErrorResult("limit must be greater than or equal to 1")

            # 将工具 path 解析为绝对路径：相对路径相对 workspace，而非进程 CWD。
            dir_path = ToolPathResolver.resolve(path, agent_ctx.workspace_path)

            # 越界保护：resolved path 必须位于 workspace 内
            boundary_err = check_path_boundary(dir_path, agent_ctx.workspace_path)
            if boundary_err:
                return ToolErrorResult(boundary_err)

            # 目录不存在
            if not dir_path.exists():
                return ToolErrorResult(f"Directory not found: {path}")
            # 目录不是目录
            if not dir_path.is_dir():
                return ToolErrorResult(f"Not a directory: {path}")
            
            # 获取目录项
            entries = []
            for child in sorted(dir_path.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir():
                    entries.append(child.name + "/")
                else:
                    entries.append(child.name)

            off = offset or 1
            lim = limit or DEFAULT_READ_LIMIT
            start = off - 1
            sliced = entries[start:start + lim]

            if start >= len(entries) and not (len(entries) == 0 and start == 0):
                return ToolErrorResult(
                    f"Offset {off} is out of range for this directory ({len(entries)} entries)"
                )

            truncated = start + len(sliced) < len(entries)
            output = "\n".join([
                f"<path>{dir_path}</path>",
                "<content>",
                "\n".join(sliced),
                "</content>",
                f"<truncated>{str(truncated).lower()}</truncated>",
                f"<next_offset>{off + len(sliced) if truncated else ''}</next_offset>",
            ])
            return ToolSuccessResult(output)
        except PermissionError as e:
            logging.error("Permission error reading directory: path=%s, error=%s", path, e)
            return ToolErrorResult(f"Error: {e}")
        except Exception as e:
            logging.error("Error reading directory: path=%s, error=%s", path, e)
            return ToolErrorResult(f"Error reading directory: {str(e)}")
