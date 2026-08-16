import logging
from pathlib import Path
from typing import Any, Dict
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ..utils import _trim_diff,_two_files_patch
from ..file_state import FILE_STATE_MANAGER
from ...contants import MEMORY_DIR_NAME
from ...schemes import AgentContext, RuntimeContext
from .lsp_diagnostics import LspFileDiagnostics
from .utils import ToolPathResolver, WindowsReservedNameGuard, check_path_boundary


@register_tool(name="write_file", toolset="filesystem")
class WriteFileTool(BaseTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    def description(self, params=None) -> str:
        return f"""Write full file content to the local filesystem.

When to use:
- Create a new file.
- Full-file rewrite / generated content where partial replace is impractical.
- Chunked writes: `mode='w'` first, then `mode='a'`.

When NOT to use:
- Small local edits in existing files (use `edit_file`).
- Structured multi-file diffs (prefer `apply_patch`).
- Creating README / docs `*.md` unless the user explicitly asks (exception: Agent memory under `{MEMORY_DIR_NAME}/`).

Execution rules:
- Required: `path`, `content`.
- `path` may be absolute or workspace-relative (resolved against the agent workspace).
- Overwriting/appending an existing file requires a prior `read_file` (offset/limit OK).
- Prefer editing existing files; create new files only when required.
- Success output includes `<already_existed>` (pre-write) and `<created>` (new file created); do not treat these as failure flags.

Failure recovery:
- Rejected for unread/stale file -> `read_file` then retry.
- Accidental full overwrite risk -> switch to `edit_file` / `apply_patch` for local changes."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative file path to write."
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file. Prefer to keep each write chunk small to avoid tool call truncation.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["w","a"],
                    "description": "w=overwrite (first chunk), a=append (subsequent chunks). Default is w."
                }
            },
            "required": ["path", "content"]
        }

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        path: str,
        content: str,
        mode: str = "w"
    ) -> ToolResult:
        try:
            if not path or not path.strip() or content is None:
                logging.error("Invalid parameters: path=%r, content=%r", path, content)
                return ToolErrorResult("Missing path or content parameter")

            open_mode = "a" if mode == "a" else "w"

            # 将工具 path 解析为绝对路径：相对路径相对 workspace，而非进程 CWD。
            target_path = ToolPathResolver.resolve(path, agent_ctx.workspace_path)

            # 越界保护：resolved path 必须位于 workspace 内
            boundary_err = check_path_boundary(target_path, agent_ctx.workspace_path)
            if boundary_err:
                return ToolErrorResult(boundary_err)

            # 拦截 Windows 保留设备名，避免在工作区生成 nul 等污染文件
            if WindowsReservedNameGuard.is_reserved_basename(target_path):
                return ToolErrorResult(WindowsReservedNameGuard.reject_message(path))
            
            async with FILE_STATE_MANAGER.get_path_lock(target_path) as resolved_path:
                file_path = Path(resolved_path)
                already_existed = file_path.exists()
                if already_existed and not file_path.is_file():
                    logging.error("Not a file: path=%s", file_path)
                    return ToolErrorResult(f"Not a file: {path}")

                if already_existed:
                    block = FILE_STATE_MANAGER.get_edit_block_reason(
                        run_ctx.actor_id,
                        file_path,
                        require_prior_read=True,
                    )
                    if block:
                        return ToolErrorResult(block)

                old_content = ""
                if already_existed and file_path.is_file():
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        old_content = f.read()

                file_path.parent.mkdir(parents=True, exist_ok=True)

                with open(file_path, open_mode, encoding="utf-8") as f:
                    f.write(content)

                new_content = ""
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    new_content = f.read()

                diff = _trim_diff(
                    _two_files_patch(str(file_path), str(file_path), old_content, new_content)
                )

                action = "appended" if open_mode == "a" else "written"
                created = (not already_existed) and open_mode == "w"
                output = "\n".join([
                    f"<path>{file_path}</path>",
                    "<content>",
                    f"Successfully {action} {len(content)} bytes to {file_path} (mode={open_mode})",
                    "</content>",
                    f"<already_existed>{str(already_existed).lower()}</already_existed>",
                    f"<created>{str(created).lower()}</created>",
                    f"<mode>{open_mode}</mode>",
                    "<diff>",
                    diff,
                    "</diff>",
                ])
                FILE_STATE_MANAGER.record_write(run_ctx.actor_id, file_path)
                diagnostics = await LspFileDiagnostics.collect(
                    str(file_path),
                    str(agent_ctx.workspace_path or "").strip(),
                )
                if diagnostics:
                    output += LspFileDiagnostics.format_suffix(diagnostics)
                return ToolSuccessResult(output, metadata={"diagnostics": diagnostics})

        except Exception as e:
            logging.error("Failed to write file: path=%r, error=%s", path, e)
            return ToolErrorResult(f"Failed to write file: {str(e)}")
