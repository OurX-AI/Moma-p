import asyncio
import difflib
import json
import re
from typing import Any, Awaitable, Dict, Optional, TypeVar
from ..schemes import RuntimeContext


_ABORT_POLL_SEC = 0.2
T = TypeVar("T")


async def await_with_abort(
    run_ctx: Optional[RuntimeContext],
    coro: Awaitable[T],
) -> Optional[T]:
    """Await coro; cancel and return None when run_ctx is aborted."""
    if run_ctx is not None and run_ctx.is_aborted():
        return None
    task = asyncio.create_task(coro)
    try:
        while not task.done():
            if run_ctx is not None and run_ctx.is_aborted():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return None
            await asyncio.sleep(_ABORT_POLL_SEC)
        return task.result()
    except asyncio.CancelledError:
        return None


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 回复中解析 JSON 对象（容忍 markdown 代码块与前后杂文本）。"""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# 文件写操作相关
def _trim_diff(diff: str) -> str:
    lines = diff.split("\n")
    content_lines = [
        ln for ln in lines
        if (ln.startswith("+") or ln.startswith("-") or ln.startswith(" "))
        and not ln.startswith("---")
        and not ln.startswith("+++")
    ]
    if not content_lines:
        return diff

    # 找到最小的缩进
    min_indent = None
    for ln in content_lines:
        content = ln[1:]
        if content.strip():
            m = re.match(r"^(\s*)", content)
            lead = len(m.group(1)) if m else 0
            min_indent = lead if min_indent is None else min(min_indent, lead)
    if not min_indent:
        return diff

    # 去除缩进
    out = []
    for ln in lines:
        if (ln.startswith("+") or ln.startswith("-") or ln.startswith(" ")) and not ln.startswith("---") and not ln.startswith("+++"):
            out.append(ln[0] + ln[1 + min_indent:])
        else:
            out.append(ln)
    return "\n".join(out)


def _two_files_patch(old_path: str, new_path: str, old_content: str, new_content: str) -> str:
    a = old_content.splitlines()
    b = new_content.splitlines()
    lines = list(difflib.unified_diff(a, b, fromfile=old_path, tofile=new_path, lineterm=""))
    return "\n".join(lines) + ("\n" if lines else "")


def not_found_message(old_text: str, content: str, path: str) -> str:
    """Build a helpful error when old_text is not found."""
    lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines(keepends=True)
    window = len(old_lines)

    best_ratio, best_start = 0.0, 0
    for i in range(max(1, len(lines) - window + 1)):
        ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, i

    if best_ratio > 0.5:
        diff = "\n".join(difflib.unified_diff(
            old_lines, lines[best_start : best_start + window],
            fromfile="old_text (provided)", tofile=f"{path} (actual, line {best_start + 1})",
            lineterm="",
        ))
        return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
    return f"Error: old_text not found in {path}. No similar text found. Verify the file content."
