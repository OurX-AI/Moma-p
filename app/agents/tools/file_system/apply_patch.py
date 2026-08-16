import difflib
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ..file_state import FILE_STATE_MANAGER
from ...schemes import AgentContext, RuntimeContext
from .lsp_diagnostics import LspFileDiagnostics
from .text_match import normalize_for_match


def trim_diff(diff: str) -> str:
    """
    压缩 unified diff 的公共缩进，提升可读性。
    仅处理内容行（+/-/空格前缀），保留文件头行不变。
    """
    lines = diff.split("\n")
    content_lines = [
        ln
        for ln in lines
        if (ln.startswith("+") or ln.startswith("-") or ln.startswith(" "))
        and not ln.startswith("---")
        and not ln.startswith("+++")
    ]
    if not content_lines:
        return diff
    min_indent: float = float("inf")
    for line in content_lines:
        content = line[1:]
        if content.strip():
            m = re.match(r"^(\s*)", content)
            lead = len(m.group(1)) if m else 0
            min_indent = min(min_indent, lead)
    if min_indent == float("inf") or min_indent == 0:
        return diff
    trimmed = []
    for line in lines:
        if (
            (line.startswith("+") or line.startswith("-") or line.startswith(" "))
            and not line.startswith("---")
            and not line.startswith("+++")
        ):
            trimmed.append(line[0] + line[1 + int(min_indent) :])
        else:
            trimmed.append(line)
    return "\n".join(trimmed)


def create_two_files_patch(
    old_path: str, new_path: str, old_content: str, new_content: str
) -> str:
    """
    基于旧内容与新内容生成 unified diff 文本。
    """
    a = old_content.splitlines()
    b = new_content.splitlines()
    lines = list(
        difflib.unified_diff(
            a,
            b,
            fromfile=old_path,
            tofile=new_path,
            lineterm="",
        )
    )
    return "\n".join(lines) + ("\n" if lines else "")


def diff_line_counts(old_content: str, new_content: str) -> Tuple[int, int]:
    """
    统计文本变更中的新增行数与删除行数。
    """
    a = old_content.split("\n")
    b = new_content.split("\n")
    sm = difflib.SequenceMatcher(None, a, b)
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            deletions += i2 - i1
            additions += j2 - j1
        elif tag == "delete":
            deletions += i2 - i1
        elif tag == "insert":
            additions += j2 - j1
    return additions, deletions


def resolve_patch_abs_path(abs_or_rel_path: str) -> Path:
    """
    仅允许 absolute 路径。
    - patch 中的 <path> / "*** Move to:" 都必须是绝对路径
    """
    raw = (abs_or_rel_path or "").strip()
    if not raw:
        raise ValueError("empty path")
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError("absolute path is required")
    return p.resolve()


Hunk = Dict[str, Any]
UpdateFileChunk = Dict[str, Any]


def strip_heredoc(input_s: str) -> str:
    """
    兼容 heredoc 输入（例如 cat <<EOF ... EOF），提取正文 patch 内容。
    非 heredoc 输入原样返回。
    """
    m = re.fullmatch(
        r"(?:cat\s+)?<<['\"]?(\w+)['\"]?\s*\n([\s\S]*?)\n\1\s*",
        input_s.strip(),
    )
    if m:
        return m.group(2)
    return input_s


def parse_patch_header(
    lines: List[str], start_idx: int
) -> Optional[Dict[str, Any]]:
    """
    解析单个文件块头部：
    - Add/Delete/Update
    - Update 场景下可解析 Move to
    """
    line = lines[start_idx]
    if line.startswith("*** Add File:"):
        file_path = line[len("*** Add File:") :].strip()
        return {"filePath": file_path, "nextIdx": start_idx + 1} if file_path else None
    if line.startswith("*** Delete File:"):
        file_path = line[len("*** Delete File:") :].strip()
        return {"filePath": file_path, "nextIdx": start_idx + 1} if file_path else None
    if line.startswith("*** Update File:"):
        file_path = line[len("*** Update File:") :].strip()
        move_path: Optional[str] = None
        next_idx = start_idx + 1
        if next_idx < len(lines) and lines[next_idx].startswith("*** Move to:"):
            move_path = lines[next_idx][len("*** Move to:") :].strip()
            next_idx += 1
        return {"filePath": file_path, "movePath": move_path, "nextIdx": next_idx} if file_path else None
    return None


def parse_update_file_chunks(lines: List[str], start_idx: int) -> Tuple[List[UpdateFileChunk], int]:
    """
    解析 Update File 区块中的 @@ chunk 列表。
    支持普通上下文行、增删行、以及 End of File 标记。
    """
    chunks: List[UpdateFileChunk] = []
    i = start_idx
    while i < len(lines) and not lines[i].startswith("***"):
        if lines[i].startswith("@@"):
            context_line = lines[i][2:].strip()
            i += 1
            old_lines: List[str] = []
            new_lines: List[str] = []
            is_end_of_file = False
            while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("***"):
                change_line = lines[i]
                if change_line == "*** End of File":
                    is_end_of_file = True
                    i += 1
                    break
                if change_line.startswith(" "):
                    content = change_line[1:]
                    old_lines.append(content)
                    new_lines.append(content)
                elif change_line.startswith("-"):
                    old_lines.append(change_line[1:])
                elif change_line.startswith("+"):
                    new_lines.append(change_line[1:])
                i += 1
            chunk: UpdateFileChunk = {
                "old_lines": old_lines,
                "new_lines": new_lines,
            }
            if context_line:
                chunk["change_context"] = context_line
            if is_end_of_file:
                chunk["is_end_of_file"] = True
            chunks.append(chunk)
        else:
            i += 1
    return chunks, i


def parse_add_file_content(lines: List[str], start_idx: int) -> Tuple[str, int]:
    """
    解析 Add File 区块正文，仅接收以 '+' 开头的新增内容行。
    """
    content = ""
    i = start_idx
    while i < len(lines) and not lines[i].startswith("***"):
        if lines[i].startswith("+"):
            content += lines[i][1:] + "\n"
        i += 1
    if content.endswith("\n"):
        content = content[:-1]
    return content, i


def parse_patch(patch_text: str) -> Dict[str, List[Hunk]]:
    """
    解析完整 patch 文本，输出标准化 hunk 列表。
    要求包含 Begin/End 包裹标记。
    """
    cleaned = strip_heredoc(patch_text.strip())
    lines = cleaned.split("\n")
    hunks: List[Hunk] = []
    begin_marker = "*** Begin Patch"
    end_marker = "*** End Patch"
    begin_idx = next((i for i, ln in enumerate(lines) if ln.strip() == begin_marker), -1)
    end_idx = next((i for i, ln in enumerate(lines) if ln.strip() == end_marker), -1)
    if begin_idx == -1 or end_idx == -1 or begin_idx >= end_idx:
        raise ValueError("Invalid patch format: missing Begin/End markers")
    i = begin_idx + 1
    while i < end_idx:
        header = parse_patch_header(lines, i)
        if not header:
            i += 1
            continue
        if lines[i].startswith("*** Add File:"):
            content, next_idx = parse_add_file_content(lines, header["nextIdx"])
            hunks.append({"type": "add", "path": header["filePath"], "contents": content})
            i = next_idx
        elif lines[i].startswith("*** Delete File:"):
            hunks.append({"type": "delete", "path": header["filePath"]})
            i = header["nextIdx"]
        elif lines[i].startswith("*** Update File:"):
            chunks, next_idx = parse_update_file_chunks(lines, header["nextIdx"])
            h: Hunk = {
                "type": "update",
                "path": header["filePath"],
                "chunks": chunks,
            }
            if header.get("movePath"):
                h["move_path"] = header["movePath"]
            hunks.append(h)
            i = next_idx
        else:
            i += 1
    return {"hunks": hunks}


def normalize_unicode(s: str) -> str:
    """归一化常见 Unicode 变体（委托 text_match.normalize_for_match）。"""
    return normalize_for_match(s)


def try_match(
    lines: List[str],
    pattern: List[str],
    start_index: int,
    compare: Callable[[str, str], bool],
    eof: bool,
) -> int:
    """
    在 lines 中从 start_index 开始查找 pattern。
    compare 控制匹配规则（精确/去空白/Unicode 归一化等）。
    当 eof=True 时，会优先尝试在文件尾部进行匹配。
    """
    if eof:
        from_end = len(lines) - len(pattern)
        if from_end >= start_index:
            matches = True
            for j in range(len(pattern)):
                if not compare(lines[from_end + j], pattern[j]):
                    matches = False
                    break
            if matches:
                return from_end
    for i in range(start_index, len(lines) - len(pattern) + 1):
        matches = True
        for j in range(len(pattern)):
            if not compare(lines[i + j], pattern[j]):
                matches = False
                break
        if matches:
            return i
    return -1


def seek_sequence(
    lines: List[str], pattern: List[str], start_index: int, eof: bool = False
) -> int:
    """
    按“由严格到宽松”的策略查找 pattern 在 lines 中的位置：
    1) 完全相等
    2) rstrip 后相等
    3) strip 后相等
    4) 引号/破折号等 Unicode 归一后相等（不 strip）
    5) strip + Unicode 归一后相等
    返回首个匹配起始下标，找不到返回 -1。
    """
    if not pattern:
        return -1
    exact = try_match(lines, pattern, start_index, lambda a, b: a == b, eof)
    if exact != -1:
        return exact
    rstrip = try_match(
        lines, pattern, start_index, lambda a, b: a.rstrip() == b.rstrip(), eof
    )
    if rstrip != -1:
        return rstrip
    trim = try_match(
        lines, pattern, start_index, lambda a, b: a.strip() == b.strip(), eof
    )
    if trim != -1:
        return trim
    quotes = try_match(
        lines,
        pattern,
        start_index,
        lambda a, b: normalize_for_match(a) == normalize_for_match(b),
        eof,
    )
    if quotes != -1:
        return quotes
    return try_match(
        lines,
        pattern,
        start_index,
        lambda a, b: normalize_for_match(a.strip()) == normalize_for_match(b.strip()),
        eof,
    )


def compute_replacements(
    original_lines: List[str], file_path: str, chunks: List[UpdateFileChunk]
) -> List[Tuple[int, int, List[str]]]:
    """
    将 patch chunk 转换成可执行的替换计划列表。
    每个计划项为 (start_idx, old_len, new_segment)：
    - start_idx: 替换起始行
    - old_len: 需要删除的旧行数
    - new_segment: 需要插入的新行列表
    """
    replacements: List[Tuple[int, int, List[str]]] = []
    line_index = 0
    for chunk in chunks:
        ctx = chunk.get("change_context")
        if ctx:
            context_idx = seek_sequence(original_lines, [ctx], line_index)
            if context_idx == -1:
                raise ValueError(f"Failed to find context '{ctx}' in {file_path}")
            line_index = context_idx + 1
        old_lines_chunk = chunk["old_lines"]
        if not old_lines_chunk:
            if original_lines and original_lines[-1] == "":
                insertion_idx = len(original_lines) - 1
            else:
                insertion_idx = len(original_lines)
            replacements.append((insertion_idx, 0, list(chunk["new_lines"])))
            continue
        pattern = list(old_lines_chunk)
        new_slice = list(chunk["new_lines"])
        eof_flag = bool(chunk.get("is_end_of_file"))
        found = seek_sequence(original_lines, pattern, line_index, eof_flag)
        if found == -1 and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found = seek_sequence(original_lines, pattern, line_index, eof_flag)
        if found != -1:
            replacements.append((found, len(pattern), new_slice))
            line_index = found + len(pattern)
        else:
            raise ValueError(
                f"Failed to find expected lines in {file_path}:\n" + "\n".join(chunk["old_lines"])
            )
    replacements.sort(key=lambda x: x[0])
    return replacements


def apply_replacements(
    lines: List[str], replacements: List[Tuple[int, int, List[str]]]
) -> List[str]:
    """
    按替换计划应用内容变更并返回新行列表。
    倒序执行替换，避免前面替换影响后续索引位置。
    """
    result = list(lines)
    for i in range(len(replacements) - 1, -1, -1):
        start_idx, old_len, new_segment = replacements[i]
        del result[start_idx : start_idx + old_len]
        for j, seg_line in enumerate(new_segment):
            result.insert(start_idx + j, seg_line)
    return result


def generate_unified_diff(old_content: str, new_content: str) -> str:
    """
    生成简化版 unified diff 片段，用于展示内容变化。
    """
    old_lines = old_content.split("\n")
    new_lines = new_content.split("\n")
    diff = "@@ -1 +1 @@\n"
    max_len = max(len(old_lines), len(new_lines))
    has_changes = False
    for i in range(max_len):
        old_line = old_lines[i] if i < len(old_lines) else ""
        new_line = new_lines[i] if i < len(new_lines) else ""
        if old_line != new_line:
            if old_line:
                diff += f"-{old_line}\n"
            if new_line:
                diff += f"+{new_line}\n"
            has_changes = True
        elif old_line:
            diff += f" {old_line}\n"
    return diff if has_changes else ""


def derive_new_contents_from_chunks(
    file_path: str, chunks: List[UpdateFileChunk]
) -> Dict[str, str]:
    """
    基于 update chunks 计算文件新内容与对应 diff。
    负责读取原文件、定位替换、应用替换，并返回：
    - content: 新文件全文
    - unified_diff: 变更摘要
    """
    try:
        original_content = Path(file_path).read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Failed to read file {file_path}: {e}") from e
    original_lines = original_content.split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines.pop()
    replacements = compute_replacements(original_lines, file_path, chunks)
    new_lines = apply_replacements(original_lines, replacements)
    if not new_lines or new_lines[-1] != "":
        new_lines.append("")
    new_content = "\n".join(new_lines)
    unified_diff = generate_unified_diff(original_content, new_content)
    return {"unified_diff": unified_diff, "content": new_content}


@register_tool(name="apply_patch", toolset="filesystem")
class ApplyPatchTool(BaseTool):
    """
    Apply patch 工具：
    - 解析 patch 文本
    - 校验路径安全
    - 预计算 diff 与变更统计
    - 执行 add/update/move/delete 落盘
    """

    @property
    def name(self) -> str:
        return "apply_patch"

    def description(self, params=None) -> str:
        return """Apply a structured multi-file patch in a file-oriented diff format.

When to use:
- Multi-file coordinated changes.
- Add/Update/Delete/Move in one atomic patch.
- Structured edits where clear hunk context matters.

When NOT to use:
- Single-file small string replace (prefer `edit_file`).
- Full-file rewrite / brand-new generated file (prefer `write_file`).

Patch envelope:
*** Begin Patch
[ one or more file sections ]
*** End Patch

File operations:
- *** Add File: <path>  (+ lines are full initial content)
- *** Delete File: <path>
- *** Update File: <path>  (optional *** Move to: <path>)

Example:
*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** End Patch

Execution rules:
- Headers are required; new lines must start with `+`.
- Paths in headers / Move-to must be absolute.
- Update/Delete/Move of existing files require a prior `read_file`.
- Prefer editing existing files; create new files only when required.

Failure recovery:
- Context miss / apply failure -> re-`read_file`, rebuild hunks from fresh content; do not replay the same stale patch.
- Single short replace keeps failing -> switch to `edit_file` with more context."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patchText": {
                    "type": "string",
                    "description": "The full patch text that describes all changes to be made",
                }
            },
            "required": ["patchText"],
        }

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        return False

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        patchText: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """
        执行 patch 主流程。
        包括：参数校验 -> patch 解析 -> 变更预验证 -> 写入文件 -> 返回变更摘要与 metadata。
        """
        try:
            if not patchText:
                return ToolErrorResult("patchText is required")
            try:
                parsed = parse_patch(patchText)
            except Exception as e:
                return ToolErrorResult(f"apply_patch verification failed: {e}")
            hunks = parsed.get("hunks") or []
            if not hunks:
                normalized = (
                    patchText.replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .strip()
                )
                if normalized == "*** Begin Patch\n*** End Patch":
                    return ToolErrorResult("patch rejected: empty patch")
                return ToolErrorResult(
                    "apply_patch verification failed: no hunks found"
                )
            file_changes: List[Dict[str, Any]] = []
            total_diff = ""
            for hunk in hunks:
                htype = hunk.get("type")
                rel = hunk.get("path", "")
                try:
                    file_path = resolve_patch_abs_path(rel)
                except ValueError as e:
                    return ToolErrorResult(f"apply_patch verification failed: {e}")
                if htype == "add":
                    old_content = ""
                    contents = hunk.get("contents") or ""
                    new_content = (
                        contents
                        if (not contents or contents.endswith("\n"))
                        else contents + "\n"
                    )
                    diff = trim_diff(
                        create_two_files_patch(
                            str(file_path), str(file_path), old_content, new_content
                        )
                    )
                    additions, deletions = diff_line_counts(old_content, new_content)
                    file_changes.append(
                        {
                            "filePath": str(file_path),
                            "oldContent": old_content,
                            "newContent": new_content,
                            "type": "add",
                            "diff": diff,
                            "additions": additions,
                            "deletions": deletions,
                        }
                    )
                    total_diff += diff + "\n"
                elif htype == "update":
                    if not file_path.is_file():
                        return ToolErrorResult(
                            f"apply_patch verification failed: Failed to read file to update: {file_path}"
                        )
                    try:
                        old_content = file_path.read_text(encoding="utf-8")
                    except OSError as e:
                        return ToolErrorResult(
                            f"apply_patch verification failed: Failed to read file to update: {file_path}: {e}"
                        )
                    try:
                        file_update = derive_new_contents_from_chunks(
                            str(file_path), hunk.get("chunks") or []
                        )
                        new_content = file_update["content"]
                    except Exception as e:
                        return ToolErrorResult(
                            f"apply_patch verification failed: {e}"
                        )
                    diff = trim_diff(
                        create_two_files_patch(
                            str(file_path), str(file_path), old_content, new_content
                        )
                    )
                    additions, deletions = diff_line_counts(old_content, new_content)
                    move_path: Optional[Path] = None
                    mp = hunk.get("move_path")
                    if mp:
                        try:
                            move_path = resolve_patch_abs_path(mp)
                        except ValueError as e:
                            return ToolErrorResult(
                                f"apply_patch verification failed: {e}"
                            )
                    file_changes.append(
                        {
                            "filePath": str(file_path),
                            "oldContent": old_content,
                            "newContent": new_content,
                            "type": "move" if move_path else "update",
                            "movePath": str(move_path) if move_path else None,
                            "diff": diff,
                            "additions": additions,
                            "deletions": deletions,
                        }
                    )
                    total_diff += diff + "\n"
                elif htype == "delete":
                    try:
                        content_to_delete = file_path.read_text(encoding="utf-8")
                    except OSError as e:
                        return ToolErrorResult(
                            f"apply_patch verification failed: {e}"
                        )
                    delete_diff = trim_diff(
                        create_two_files_patch(
                            str(file_path), str(file_path), content_to_delete, ""
                        )
                    )
                    deletions = len(content_to_delete.split("\n"))
                    file_changes.append(
                        {
                            "filePath": str(file_path),
                            "oldContent": content_to_delete,
                            "newContent": "",
                            "type": "delete",
                            "diff": delete_diff,
                            "additions": 0,
                            "deletions": deletions,
                        }
                    )
                    total_diff += delete_diff + "\n"
                else:
                    return ToolErrorResult(
                        f"apply_patch verification failed: unknown hunk type {htype!r}"
                    )
            files_out: List[Dict[str, Any]] = []
            for change in file_changes:
                target = change.get("movePath") or change["filePath"]
                rel_disp = str(Path(target).resolve()).replace("\\", "/")
                entry: Dict[str, Any] = {
                    "filePath": change["filePath"],
                    "relativePath": rel_disp,
                    "type": change["type"],
                    "diff": change["diff"],
                    "before": change["oldContent"],
                    "after": change["newContent"],
                    "additions": change["additions"],
                    "deletions": change["deletions"],
                }
                if change.get("movePath"):
                    entry["movePath"] = change["movePath"]
                files_out.append(entry)

            actor_id = run_ctx.actor_id or ""
            guarded_paths: List[str] = []
            for change in file_changes:
                ctype = change["type"]
                if ctype in ("update", "delete", "move"):
                    guarded_paths.append(change["filePath"])
                if ctype == "move" and change.get("movePath"):
                    guarded_paths.append(str(change["movePath"]))
            for path in guarded_paths:
                block = FILE_STATE_MANAGER.get_edit_block_reason(
                    actor_id,
                    path,
                    require_prior_read=True,
                )
                if block:
                    return ToolErrorResult(block)

            lock_paths: List[str] = []
            for change in file_changes:
                lock_paths.append(change["filePath"])
                if change.get("movePath"):
                    lock_paths.append(str(change["movePath"]))

            async with FILE_STATE_MANAGER.get_paths_lock(lock_paths):
                for change in file_changes:
                    ctype = change["type"]
                    fp = Path(change["filePath"])
                    edited: Optional[Path] = None
                    if ctype == "add":
                        fp.parent.mkdir(parents=True, exist_ok=True)
                        fp.write_text(change["newContent"], encoding="utf-8")
                        edited = fp
                    elif ctype == "update":
                        fp.write_text(change["newContent"], encoding="utf-8")
                        edited = fp
                    elif ctype == "move":
                        mp = change.get("movePath")
                        if mp:
                            mpp = Path(mp)
                            mpp.parent.mkdir(parents=True, exist_ok=True)
                            mpp.write_text(change["newContent"], encoding="utf-8")
                            fp.unlink()
                            edited = mpp
                    elif ctype == "delete":
                        try:
                            deleted_mtime = fp.stat().st_mtime
                        except OSError:
                            deleted_mtime = None
                        fp.unlink()
                        if deleted_mtime is not None:
                            FILE_STATE_MANAGER.record_write(
                                actor_id, fp, mtime=float(deleted_mtime)
                            )
                    if edited is not None:
                        FILE_STATE_MANAGER.record_write(actor_id, edited)
                        logging.debug("apply_patch edited %s", edited)

            summary_lines = []
            changed_files_for_lsp: List[str] = []
            for change in file_changes:
                ctype = change["type"]
                if ctype == "add":
                    rp = str(Path(change["filePath"]).resolve()).replace("\\", "/")
                    summary_lines.append(f"A {rp}")
                    changed_files_for_lsp.append(str(Path(change["filePath"]).resolve()))
                elif ctype == "delete":
                    rp = str(Path(change["filePath"]).resolve()).replace("\\", "/")
                    summary_lines.append(f"D {rp}")
                else:
                    tgt = change.get("movePath") or change["filePath"]
                    rp = str(Path(tgt).resolve()).replace("\\", "/")
                    summary_lines.append(f"M {rp}")
                    changed_files_for_lsp.append(str(Path(tgt).resolve()))
            output = (
                "Success. Updated the following files:\n" + "\n".join(summary_lines)
            )
            workspace_root = str(agent_ctx.workspace_path or "").strip()
            diagnostics = await LspFileDiagnostics.collect(
                changed_files_for_lsp,
                workspace_root,
            )
            if diagnostics:
                output += LspFileDiagnostics.format_suffix(diagnostics)
            return ToolSuccessResult(
                output,
                metadata={"diff": total_diff, "files": files_out, "diagnostics": diagnostics},
            )
        except Exception as e:
            logging.error("apply_patch execution error: %s", e)
            return ToolErrorResult(f"apply_patch error: {e}")
