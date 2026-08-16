import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from ..base import BaseTool
from ..catalog import register_tool
from ..file_state import FILE_STATE_MANAGER
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ..utils import _trim_diff, _two_files_patch, not_found_message
from ...schemes import AgentContext, RuntimeContext
from .lsp_diagnostics import LspFileDiagnostics
from .text_match import find_actual_string, preserve_quote_style
from .utils import ToolPathResolver, WindowsReservedNameGuard, check_path_boundary


def _line_trimmed_candidates(content: str, search: str) -> List[str]:
    target_lines = search.split("\n")
    content_lines = content.split("\n")
    if not target_lines or len(content_lines) < len(target_lines):
        return []
    out: List[str] = []
    width = len(target_lines)
    for i in range(0, len(content_lines) - width + 1):
        block = content_lines[i : i + width]
        ok = True
        for j in range(width):
            if block[j].strip() != target_lines[j].strip():
                ok = False
                break
        if ok:
            out.append("\n".join(block))
    return out


def _remove_common_indent(text: str) -> str:
    lines = text.split("\n")
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return text
    min_indent = min(len(ln) - len(ln.lstrip(" \t")) for ln in non_empty)
    if min_indent <= 0:
        return text
    out: List[str] = []
    for ln in lines:
        if ln.strip():
            out.append(ln[min_indent:])
        else:
            out.append(ln)
    return "\n".join(out)


def _indent_flexible_candidates(content: str, search: str) -> List[str]:
    target_norm = _remove_common_indent(search)
    target_lines = search.split("\n")
    content_lines = content.split("\n")
    if not target_lines or len(content_lines) < len(target_lines):
        return []
    out: List[str] = []
    width = len(target_lines)
    for i in range(0, len(content_lines) - width + 1):
        block = "\n".join(content_lines[i : i + width])
        if _remove_common_indent(block) == target_norm:
            out.append(block)
    return out


def _dedupe_ordered(values: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in values:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _block_anchor_candidates(content: str, search: str) -> List[str]:
    """首尾行锚点匹配"""
    original_lines = content.split("\n")
    search_lines = search.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines = search_lines[:-1]
    if len(search_lines) < 3:
        return []
    first = search_lines[0].strip()
    last = search_lines[-1].strip()
    search_size = len(search_lines)
    max_delta = max(1, search_size // 4)
    candidates: List[tuple[int, int]] = []
    for i, line in enumerate(original_lines):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() != last:
                continue
            actual = j - i + 1
            if abs(actual - search_size) <= max_delta:
                candidates.append((i, j))
            break
    if not candidates:
        return []
    out: List[str] = []
    for start, end in candidates:
        actual = end - start + 1
        mid_n = min(search_size - 2, actual - 2)
        similarity = 1.0
        if mid_n > 0:
            score = 0.0
            for k in range(1, mid_n + 1):
                o = original_lines[start + k].strip()
                s = search_lines[k].strip()
                max_len = max(len(o), len(s), 1)
                score += 1.0 - (_levenshtein(o, s) / max_len)
            similarity = score / mid_n
        # 单候选放宽，多候选更严
        threshold = 0.0 if len(candidates) == 1 else 0.3
        if similarity >= threshold:
            out.append("\n".join(original_lines[start : end + 1]))
    return out


def _resolve_actual_old(content: str, old_string: str) -> tuple[str | None, str]:
    exact_count = content.count(old_string)
    if exact_count > 0:
        return old_string, "exact"
    candidates: List[str] = []
    normalized_hit = find_actual_string(content, old_string)
    if normalized_hit:
        candidates.append(normalized_hit)
    candidates.extend(_line_trimmed_candidates(content, old_string))
    candidates.extend(_indent_flexible_candidates(content, old_string))
    candidates.extend(_block_anchor_candidates(content, old_string))
    uniq = _dedupe_ordered(candidates)
    if not uniq:
        return None, "not_found"
    if len(uniq) > 1:
        return None, "ambiguous_fuzzy"
    return uniq[0], "fuzzy"


@register_tool(name="edit_file", toolset="filesystem")
class EditFileTool(BaseTool):
    """Edit file by replacing old_string with new_string."""

    @property
    def name(self) -> str:
        return "edit_file"

    def description(self, params=None) -> str:
        return """Edit file content via targeted string replacement.

When to use:
- Small, local edits inside an existing file.
- Precise replacement with explicit `old_string` -> `new_string`.

When NOT to use:
- Full-file rewrite (use `write_file`).
- Large structural multi-file refactors (prefer `apply_patch`).

Execution rules:
- Required params: `path`, `old_string`, `new_string`.
- `path` may be absolute or workspace-relative (resolved against the agent workspace, not process CWD).
- You MUST `read_file` the target file before editing; stale/unread content is rejected.
- Default behavior replaces one unique match only.
- `replace_all=true` is allowed only for exact matches.

Matching behavior:
- Try exact match first.
- If exact miss, fallback to limited fuzzy matching (quote normalization, line-trim, indent-flex, block-anchor).
- If fuzzy match is ambiguous, edit is rejected to prevent accidental changes.
- Success reports `match_strategy=exact|fuzzy`. Fuzzy can hit a near-miss block — always inspect the returned diff; if wrong, re-`read_file` and retry with more surrounding context (prefer exact).

Failure recovery:
- Not found -> read the latest file and include more surrounding context in `old_string`.
- Multiple matches -> expand `old_string` context or use `replace_all=true` for exact global replace.
- Fuzzy but wrong edit -> do not keep stacking fuzzy retries; widen exact `old_string` context."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or workspace-relative file path"},
                "old_string": {"type": "string", "description": "Text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences of old_string"},
            },
            "required": ["path", "old_string", "new_string"],
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
        old_string: str,
        new_string: str,
        replace_all: Optional[bool] = None,
    ) -> ToolResult:
        try:
            if not path or not str(path).strip():
                return ToolErrorResult("Missing required parameter: path")
            if old_string is None or new_string is None:
                return ToolErrorResult("Missing required parameter: old_string or new_string")
            if old_string == "":
                return ToolErrorResult("old_string must not be empty. Use write_file for full overwrite.")
            if old_string == new_string:
                return ToolErrorResult("No changes to apply: old_string and new_string are identical.")

            # 将工具 path 解析为绝对路径：相对路径相对 workspace，而非进程 CWD。
            target_path = ToolPathResolver.resolve(path, agent_ctx.workspace_path)

            # 越界保护：resolved path 必须位于 workspace 内
            boundary_err = check_path_boundary(target_path, agent_ctx.workspace_path)
            if boundary_err:
                return ToolErrorResult(boundary_err)

            # 文件不存在
            if not target_path.exists():
                return ToolErrorResult(f"File not found: {path}")

            if WindowsReservedNameGuard.is_reserved_basename(target_path):
                return ToolErrorResult(WindowsReservedNameGuard.reject_message(path))
            async with FILE_STATE_MANAGER.get_path_lock(target_path) as resolved_path:
                file_path = Path(resolved_path)
                if not file_path.exists():
                    return ToolErrorResult(f"File not found: {path}")
                if not file_path.is_file():
                    return ToolErrorResult(f"Not a file: {path}")

                block = FILE_STATE_MANAGER.get_edit_block_reason(
                    run_ctx.actor_id,
                    file_path,
                    require_prior_read=True,
                )
                if block:
                    return ToolErrorResult(block)

                content = file_path.read_text(encoding="utf-8", errors="replace")
                exact_count = content.count(old_string)

                actual_old, match_strategy = _resolve_actual_old(content, old_string)
                if actual_old is None:
                    if match_strategy == "ambiguous_fuzzy":
                        return ToolErrorResult(
                            "Could not locate a unique match for old_string (fuzzy match found multiple candidates). "
                            "Please provide more surrounding context."
                        )
                    return ToolErrorResult(not_found_message(old_string, content, path))

                actual_new = preserve_quote_style(old_string, actual_old, new_string)
                count = content.count(actual_old)
                do_replace_all = bool(replace_all)

                if do_replace_all and match_strategy != "exact":
                    return ToolErrorResult(
                        "replace_all=true requires an exact old_string match. "
                        "Please provide exact old_string to avoid over-replacing."
                    )

                if not do_replace_all:
                    if count > 1:
                        # exact or fuzzy candidate appears multiple times
                        if exact_count > 1 and actual_old == old_string:
                            return ToolErrorResult(
                                f"old_string appears {exact_count} times. "
                                "Please provide more context to make it unique or set replace_all=true."
                            )
                        return ToolErrorResult(
                            f"Matched text appears {count} times after normalization. "
                            "Please provide more context to make it unique."
                        )
                    new_content = content.replace(actual_old, actual_new, 1)
                    replaced_count = 1
                else:
                    new_content = content.replace(actual_old, actual_new)
                    replaced_count = count

                if new_content == content:
                    return ToolErrorResult("No changes to apply.")

                file_path.write_text(new_content, encoding="utf-8")
                diff = _trim_diff(_two_files_patch(str(file_path), str(file_path), content, new_content))
                FILE_STATE_MANAGER.record_write(run_ctx.actor_id, file_path)

                diagnostics = await LspFileDiagnostics.collect(
                    str(file_path),
                    str(agent_ctx.workspace_path or "").strip(),
                )
                output = "\n".join(
                    [
                        f"<path>{file_path}</path>",
                        "<content>",
                        (
                            f"Successfully edited {path} "
                            f"(replaced {replaced_count} occurrence{'s' if replaced_count != 1 else ''}, "
                            f"match_strategy={match_strategy})"
                        ),
                        "</content>",
                        "<diff>",
                        diff,
                        "</diff>",
                    ]
                )
                if diagnostics:
                    output += LspFileDiagnostics.format_suffix(diagnostics)

                return ToolSuccessResult(
                    output,
                    metadata={"diagnostics": diagnostics, "match_strategy": match_strategy},
                )
        except PermissionError as e:
            logging.error("Permission error on edit_file path=%r: %s", path, e)
            return ToolErrorResult(f"Permission error: {e}")
        except Exception as e:
            logging.error("Failed edit_file path=%r: %s", path, e)
            return ToolErrorResult(f"Failed to edit file: {e}")
