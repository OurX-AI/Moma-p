import asyncio
import fnmatch
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ..base import BaseTool
from ..catalog import register_tool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from .utils import resolve_search_dir, rg_executable


_BRACE_SEGMENT = re.compile(r"\{([^{}]+)\}")
_YIELD_EVERY_N_FILES = 32


def _expand_brace_patterns(pattern: str) -> List[str]:
    m = _BRACE_SEGMENT.search(pattern)
    if not m:
        return [pattern]
    inner = m.group(1)
    alts = [x.strip() for x in inner.split(",") if x.strip()]
    if not alts:
        return [pattern]
    prefix, suffix = pattern[: m.start()], pattern[m.end() :]
    out: List[str] = []
    for alt in alts:
        out.extend(_expand_brace_patterns(prefix + alt + suffix))
    return out


def _effective_patterns(pattern: str) -> List[str]:
    raw = _expand_brace_patterns(pattern)
    seen = list(dict.fromkeys(raw))
    for pat in list(seen):
        if pat.startswith("**/") and len(pat) > 3:
            tail = pat[3:]
            if tail and tail not in seen:
                seen.append(tail)
    return seen


async def _glob_ripgrep(
    search: Path,
    patterns: List[str],
    limit: int,
    run_ctx: RuntimeContext,
) -> Optional[Tuple[List[Tuple[str, float]], bool]]:
    rg = rg_executable()
    if not rg:
        return None

    cmd: List[str] = [rg, "--files", "--color=never"]
    for pat in patterns:
        cmd.extend(["-g", pat])
    cmd.append(".")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(search),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logging.warning("glob_search: failed to start ripgrep, fallback to Python: %s", e)
        return None

    stdout = process.stdout
    if stdout is None:
        process.kill()
        await process.wait()
        return None

    rel_paths: List[str] = []
    while True:
        if run_ctx.is_aborted():
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return None
        line = await stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            rel_paths.append(text.replace("\\", "/"))

    stderr_bytes = b""
    if process.stderr is not None:
        stderr_bytes = await process.stderr.read()
    returncode = await process.wait()
    if returncode not in (0, 1):
        err = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"ripgrep exited with code {returncode}")

    items: List[Tuple[str, float]] = []
    for rel in rel_paths:
        fp = (search / rel).resolve()
        if not fp.is_file():
            continue
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        items.append((str(fp), mtime))

    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit], len(items) > limit


async def _glob_python(
    search: Path,
    patterns: List[str],
    limit: int,
    run_ctx: RuntimeContext,
) -> Optional[Tuple[List[Tuple[str, float]], bool]]:
    items: List[Tuple[str, float]] = []
    scanned = 0
    for p in search.rglob("*"):
        if run_ctx.is_aborted():
            return None
        scanned += 1
        if scanned % _YIELD_EVERY_N_FILES == 0:
            await asyncio.sleep(0)
        if not p.is_file():
            continue
        rel = str(p.relative_to(search)).replace("\\", "/")
        if any(
            fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(p.name, pat)
            for pat in patterns
        ):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            items.append((str(p), mtime))
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit], len(items) > limit


@register_tool(name="glob_search", toolset="filesystem")
class GlobSearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "glob_search"

    def description(self, params=None) -> str:
        backend = "ripgrep --files" if rg_executable() else "Python fnmatch walk"
        return f"""Find files by name/path pattern.

Backend: {backend}.

When to use:
- Locate files by extension, basename, or path glob before reading.

When NOT to use:
- Search inside file contents (use `grep_search`).
- List one known directory (use `read_dir`).

Matching:
- Wildcards: * ? [abc]; brace expansion like `*.{{py,toml}}`; leading `**/` supported.
- Newest first by mtime; at most 100 paths returned.

Failure recovery:
- Zero hits -> broaden pattern or raise search root `path`.
- Too many hits -> narrow pattern/`path`, then `read_file` only top candidates."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "fnmatch-style pattern. Wildcards: *, ?, [seq]. "
                        "Brace alternation: *.{py,toml}. Leading **/: e.g. **/*.js."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative directory to search; defaults to agent workspace.",
                },
            },
            "required": ["pattern"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        pattern: str,
        path: Optional[str] = None
    ) -> ToolResult:
        if not pattern:
            return ToolErrorResult("pattern is required")

        try:
            search = resolve_search_dir(path, agent_ctx.workspace_path or "")
            if not search.exists() or not search.is_dir():
                return ToolErrorResult(f"glob failed: directory does not exist: {search}")
        except ValueError as e:
            return ToolErrorResult(f"glob failed: {e}")
        except Exception as e:
            return ToolErrorResult(f"glob failed: {e}")

        limit = 100
        patterns = _effective_patterns(pattern)
        try:
            rg_result = await _glob_ripgrep(search, patterns, limit, run_ctx)
            if rg_result is None:
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                py_result = await _glob_python(search, patterns, limit, run_ctx)
                if py_result is None:
                    if run_ctx.is_aborted():
                        return run_ctx.aborted_tool_result(self.name)
                    return ToolErrorResult("glob failed: no result")
                final, truncated = py_result
            else:
                final, truncated = rg_result
        except Exception as e:
            return ToolErrorResult(f"glob failed: {e}")

        if not final:
            return ToolSuccessResult("No files found")

        out = "\n".join([p for p, _ in final])
        if truncated:
            out += "\n\n(Results are truncated: showing 100 newest by mtime. Narrow `path` or use a more specific pattern.)"
        return ToolSuccessResult(out)
