import asyncio
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from ..base import BaseTool
from ..catalog import register_tool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from .grep_options import DEFAULT_HEAD_LIMIT, GrepQuery, GrepResultWindow
from .utils import resolve_search_dir, rg_executable


MAX_LINE_LENGTH = 2000
_YIELD_EVERY_N_FILES = 32
OutputMode = Literal["content", "files_with_matches", "count"]
MatchRow = Tuple[str, float, int, str, str]


def _include_match(path: Path, include: Optional[str]) -> bool:
    if not include:
        return True
    inc = include
    if inc.startswith("{") and inc.endswith("}"):
        inc = "*." + inc[1:-1]
    if "{" in inc and "}" in inc:
        parts = [part.strip() for part in inc.split(",") if part.strip()]
        return any(Path(path.name).match(part) for part in parts)
    return Path(path.name).match(inc) or Path(str(path)).match(inc)


def _truncate_line(text: str) -> str:
    if len(text) > MAX_LINE_LENGTH:
        return text[:MAX_LINE_LENGTH] + "..."
    return text


def _format_content_output(
    matches: List[MatchRow],
    *,
    total: int,
    shown_limit: int,
    offset: int,
) -> str:
    if not matches and total == 0:
        return "No files found"
    if not matches:
        return f"No matches in requested window (offset={offset}, total={total})"
    truncated = offset + len(matches) < total or (shown_limit and total > offset + shown_limit)
    header = f"Found {total} matches"
    if truncated or offset:
        header += f" (showing {len(matches)}"
        if offset:
            header += f" from offset {offset}"
        header += ")"
    out_lines = [header]
    current = ""
    for fp, _, ln, text, kind in matches:
        if current != fp:
            if current:
                out_lines.append("")
            current = fp
            out_lines.append(f"{fp}:")
        mark = ":" if kind == "match" else "-"
        out_lines.append(f"  Line {ln}{mark} {_truncate_line(text)}")
    if truncated:
        out_lines.append("")
        out_lines.append(
            f"(Results truncated: use head_limit/offset or a narrower path/pattern. "
            f"head_limit=0 raises the safety cap.)"
        )
    return "\n".join(out_lines)


def _format_files_output(
    rows: List[Tuple[str, float]],
    *,
    total: int,
    offset: int,
) -> str:
    if not rows and total == 0:
        return "No files found"
    if not rows:
        return f"No files in requested window (offset={offset}, total={total})"
    truncated = offset + len(rows) < total
    header = f"Found {total} files"
    if truncated or offset:
        header += f" (showing {len(rows)}"
        if offset:
            header += f" from offset {offset}"
        header += ")"
    out_lines = [header]
    out_lines.extend(fp for fp, _ in rows)
    if truncated:
        out_lines.append("")
        out_lines.append("(Results truncated. Use head_limit/offset or a more specific path/pattern.)")
    return "\n".join(out_lines)


def _format_count_output(
    rows: List[Tuple[str, float, int]],
    *,
    total: int,
    offset: int,
) -> str:
    if not rows and total == 0:
        return "No files found"
    if not rows:
        return f"No files in requested window (offset={offset}, total={total})"
    truncated = offset + len(rows) < total
    header = f"Found {total} files with matches"
    if truncated or offset:
        header += f" (showing {len(rows)}"
        if offset:
            header += f" from offset {offset}"
        header += ")"
    out_lines = [header]
    for fp, _, count in rows:
        out_lines.append(f"{fp}: {count}")
    if truncated:
        out_lines.append("")
        out_lines.append("(Results truncated. Use head_limit/offset or a more specific path/pattern.)")
    return "\n".join(out_lines)


def _iter_files(search: Path, single_file: Optional[Path], include: Optional[str]):
    if single_file is not None:
        if single_file.is_file() and _include_match(single_file, include):
            yield single_file
        return
    for fp in search.rglob("*"):
        if fp.is_file() and _include_match(fp, include):
            yield fp


def _rg_common_flags(query: GrepQuery) -> List[str]:
    flags: List[str] = ["--color=never"]
    if query.case_insensitive:
        flags.append("-i")
    if query.multiline:
        flags.extend(["-U", "--multiline-dotall"])
    if query.include:
        flags.extend(["-g", query.include])
    elif query.file_type and query.file_type.strip():
        flags.extend(["--type", query.file_type.strip()])
    return flags


def _rg_context_args(query: GrepQuery) -> List[str]:
    c, b, a = query.context_flags()
    args: List[str] = []
    if c is not None:
        args.extend(["-C", str(c)])
    else:
        if b is not None:
            args.extend(["-B", str(b)])
        if a is not None:
            args.extend(["-A", str(a)])
    return args


def _rg_targets(search: Path, single_file: Optional[Path]) -> List[str]:
    if single_file is not None:
        return [str(single_file)]
    return ["."]


async def _run_rg_lines(
    cmd: List[str],
    cwd: Path,
    run_ctx: RuntimeContext,
) -> Optional[Tuple[int, bytes]]:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logging.warning("grep_search: failed to start ripgrep: %s", e)
        return None

    stdout = process.stdout
    if stdout is None:
        process.kill()
        await process.wait()
        return None

    chunks: List[bytes] = []
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
        chunks.append(line)

    stderr_bytes = b""
    if process.stderr is not None:
        stderr_bytes = await process.stderr.read()
    returncode = await process.wait()
    if returncode not in (0, 1):
        err = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"ripgrep exited with code {returncode}")
    return returncode, b"".join(chunks)


async def _scan_content_python(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    rx: re.Pattern[str],
    run_ctx: RuntimeContext,
) -> Optional[List[MatchRow]]:
    include = query.resolved_include()
    c, b, a = query.context_flags()
    before_n = c if c is not None else (b or 0)
    after_n = c if c is not None else (a or 0)
    matches: List[MatchRow] = []
    scanned = 0
    for fp in _iter_files(search, single_file, include):
        if run_ctx.is_aborted():
            return None
        scanned += 1
        if scanned % _YIELD_EVERY_N_FILES == 0:
            await asyncio.sleep(0)
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if query.multiline:
            text = "\n".join(lines)
            for m in rx.finditer(text):
                start = text.count("\n", 0, m.start()) + 1
                snippet = _truncate_line(m.group(0).replace("\n", "\\n"))
                matches.append((str(fp.resolve()), mtime, start, snippet, "match"))
            continue
        hit_indexes = [idx for idx, line in enumerate(lines) if rx.search(line)]
        emitted: set[int] = set()
        for idx in hit_indexes:
            lo = max(0, idx - before_n)
            hi = min(len(lines), idx + after_n + 1)
            for j in range(lo, hi):
                if j in emitted:
                    continue
                emitted.add(j)
                kind = "match" if j == idx or (j in hit_indexes) else "context"
                if j in hit_indexes:
                    kind = "match"
                matches.append(
                    (str(fp.resolve()), mtime, j + 1, _truncate_line(lines[j]), kind)
                )
    matches.sort(key=lambda x: (x[1], x[0], x[2]), reverse=True)
    return matches


async def _scan_files_python(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    rx: re.Pattern[str],
    run_ctx: RuntimeContext,
) -> Optional[List[Tuple[str, float]]]:
    include = query.resolved_include()
    files: Dict[str, float] = {}
    scanned = 0
    for fp in _iter_files(search, single_file, include):
        if run_ctx.is_aborted():
            return None
        scanned += 1
        if scanned % _YIELD_EVERY_N_FILES == 0:
            await asyncio.sleep(0)
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if query.multiline:
            if rx.search(text):
                files[str(fp.resolve())] = mtime
            continue
        for line in text.splitlines():
            if rx.search(line):
                files[str(fp.resolve())] = mtime
                break
    return sorted(files.items(), key=lambda x: x[1], reverse=True)


async def _scan_count_python(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    rx: re.Pattern[str],
    run_ctx: RuntimeContext,
) -> Optional[List[Tuple[str, float, int]]]:
    include = query.resolved_include()
    counts: Counter[str] = Counter()
    mtimes: Dict[str, float] = {}
    scanned = 0
    for fp in _iter_files(search, single_file, include):
        if run_ctx.is_aborted():
            return None
        scanned += 1
        if scanned % _YIELD_EVERY_N_FILES == 0:
            await asyncio.sleep(0)
        key = str(fp.resolve())
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if query.multiline:
            n = len(rx.findall(text))
            if n:
                counts[key] = n
                mtimes[key] = mtime
            continue
        n = 0
        for line in text.splitlines():
            if rx.search(line):
                n += 1
        if n:
            counts[key] = n
            mtimes[key] = mtime
    rows = sorted(
        [(p, mtimes[p], c) for p, c in counts.items()],
        key=lambda x: (x[1], x[0]),
        reverse=True,
    )
    return rows


async def _scan_content_ripgrep(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    run_ctx: RuntimeContext,
) -> Optional[List[MatchRow]]:
    rg = rg_executable()
    if not rg:
        return None
    cmd: List[str] = [rg, "--json", "--line-number", "--no-heading", *_rg_common_flags(query)]
    cmd.extend(_rg_context_args(query))
    cmd.append(query.pattern)
    cmd.extend(_rg_targets(search, single_file))
    cwd = search if single_file is None else single_file.parent
    result = await _run_rg_lines(cmd, cwd, run_ctx)
    if result is None:
        return None
    _, raw = result
    matches: List[MatchRow] = []
    mtime_cache: Dict[str, float] = {}
    for line in raw.splitlines():
        try:
            payload = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        kind = payload.get("type")
        if kind not in ("match", "context"):
            continue
        data = payload.get("data") or {}
        fp = (data.get("path") or {}).get("text") or ""
        if not fp:
            continue
        path_obj = Path(fp)
        if not path_obj.is_absolute():
            path_obj = (cwd / path_obj).resolve()
            fp = str(path_obj)
        line_number = int(data.get("line_number") or 0)
        text = _truncate_line(((data.get("lines") or {}).get("text") or "").rstrip("\n\r"))
        if fp not in mtime_cache:
            try:
                mtime_cache[fp] = Path(fp).stat().st_mtime
            except OSError:
                mtime_cache[fp] = 0.0
        matches.append((fp, mtime_cache[fp], line_number, text, "match" if kind == "match" else "context"))
    matches.sort(key=lambda x: (x[1], x[0], x[2]), reverse=True)
    return matches


async def _scan_files_ripgrep(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    run_ctx: RuntimeContext,
) -> Optional[List[Tuple[str, float]]]:
    rg = rg_executable()
    if not rg:
        return None
    cmd: List[str] = [rg, "-l", *_rg_common_flags(query), query.pattern, *_rg_targets(search, single_file)]
    cwd = search if single_file is None else single_file.parent
    result = await _run_rg_lines(cmd, cwd, run_ctx)
    if result is None:
        return None
    _, raw = result
    paths = [ln.decode("utf-8", errors="replace").strip() for ln in raw.splitlines() if ln.strip()]
    rows: List[Tuple[str, float]] = []
    for rel in paths:
        fp = Path(rel)
        if not fp.is_absolute():
            fp = (cwd / rel).resolve()
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append((str(fp.resolve()), mtime))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows


async def _scan_count_ripgrep(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    run_ctx: RuntimeContext,
) -> Optional[List[Tuple[str, float, int]]]:
    rg = rg_executable()
    if not rg:
        return None
    cmd: List[str] = [rg, "-c", *_rg_common_flags(query), query.pattern, *_rg_targets(search, single_file)]
    cwd = search if single_file is None else single_file.parent
    result = await _run_rg_lines(cmd, cwd, run_ctx)
    if result is None:
        return None
    _, raw = result
    rows: List[Tuple[str, float, int]] = []
    for line in raw.splitlines():
        text = line.decode("utf-8", errors="replace").strip()
        if not text or ":" not in text:
            continue
        fp_part, count_part = text.rsplit(":", 1)
        try:
            count = int(count_part.strip())
        except ValueError:
            continue
        fp = Path(fp_part.strip())
        if not fp.is_absolute():
            fp = (cwd / fp).resolve()
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append((str(fp), mtime, count))
    rows.sort(key=lambda x: (x[1], x[0]), reverse=True)
    return rows


async def _grep_execute(
    search: Path,
    single_file: Optional[Path],
    query: GrepQuery,
    rx: re.Pattern[str],
    output_mode: OutputMode,
    run_ctx: RuntimeContext,
) -> Optional[str]:
    try:
        if output_mode == "content":
            rows = await _scan_content_ripgrep(search, single_file, query, run_ctx)
            if rows is None:
                rows = await _scan_content_python(search, single_file, query, rx, run_ctx)
            if rows is None:
                return None
            window, total, limit = GrepResultWindow.apply(
                rows, offset=query.offset, head_limit=query.head_limit
            )
            return _format_content_output(window, total=total, shown_limit=limit, offset=query.offset)

        if output_mode == "files_with_matches":
            rows = await _scan_files_ripgrep(search, single_file, query, run_ctx)
            if rows is None:
                rows = await _scan_files_python(search, single_file, query, rx, run_ctx)
            if rows is None:
                return None
            window, total, _ = GrepResultWindow.apply(
                rows, offset=query.offset, head_limit=query.head_limit
            )
            return _format_files_output(window, total=total, offset=query.offset)

        rows = await _scan_count_ripgrep(search, single_file, query, run_ctx)
        if rows is None:
            rows = await _scan_count_python(search, single_file, query, rx, run_ctx)
        if rows is None:
            return None
        window, total, _ = GrepResultWindow.apply(
            rows, offset=query.offset, head_limit=query.head_limit
        )
        return _format_count_output(window, total=total, offset=query.offset)
    except Exception as e:
        logging.warning("grep_search: ripgrep failed, fallback to Python: %s", e)
        if output_mode == "content":
            rows = await _scan_content_python(search, single_file, query, rx, run_ctx)
            if rows is None:
                return None
            window, total, limit = GrepResultWindow.apply(
                rows, offset=query.offset, head_limit=query.head_limit
            )
            return _format_content_output(window, total=total, shown_limit=limit, offset=query.offset)
        if output_mode == "files_with_matches":
            rows = await _scan_files_python(search, single_file, query, rx, run_ctx)
            if rows is None:
                return None
            window, total, _ = GrepResultWindow.apply(
                rows, offset=query.offset, head_limit=query.head_limit
            )
            return _format_files_output(window, total=total, offset=query.offset)
        rows = await _scan_count_python(search, single_file, query, rx, run_ctx)
        if rows is None:
            return None
        window, total, _ = GrepResultWindow.apply(
            rows, offset=query.offset, head_limit=query.head_limit
        )
        return _format_count_output(window, total=total, offset=query.offset)


def _resolve_search_target(path: Optional[str], workspace_path: str) -> Tuple[Path, Optional[Path]]:
    """返回 (search_dir, single_file|None)。允许 path 为目录或文件。"""
    search = resolve_search_dir(path, workspace_path)
    if search.is_file():
        return search.parent, search
    return search, None


@register_tool(name="grep_search", toolset="filesystem")
class GrepSearchTool(BaseTool):
    @property
    def name(self) -> str:
        return "grep_search"

    def description(self, params=None) -> str:
        backend = "ripgrep (rg)" if rg_executable() else "built-in Python scanner"
        return f"""Search file contents with regular expressions.

Backend: {backend}.

When to use:
- Find symbols, strings, configs, error messages across the repo.
- Narrow with `include`/`glob`/`type` before broad reads.

When NOT to use:
- Find files by name only (use `glob_search`).
- Read a known file region (use `read_file`).
- Optional: if `codebase_symbol_locate` is in the tool list AND embedding-ready, prefer it for semantic locate; otherwise use this tool (do not wait on codebase).

Execution rules:
- `output_mode`: content | files_with_matches | count.
- Prefer scoped `path` + file filters over whole-repo blind search.
- Paginate with `head_limit` (default {DEFAULT_HEAD_LIMIT}; 0 = high safety cap) and `offset`.

Failure recovery:
- Zero hits -> relax pattern, toggle `case_insensitive`, broaden glob, or switch to `glob_search` + `read_file`.
- Too many hits -> tighten pattern/path/glob before reading."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regex pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "Optional absolute or workspace-relative directory/file path; defaults to agent workspace.",
                },
                "include": {
                    "type": "string",
                    "description": "File glob to include (e.g. *.py, *.{ts,tsx}). Alias of glob.",
                },
                "glob": {
                    "type": "string",
                    "description": "Alias of include. File glob to include.",
                },
                "type": {
                    "type": "string",
                    "description": "File type for ripgrep --type (e.g. py, js, ts, rust). Ignored if include/glob set.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Result format: matching lines, file paths only, or per-file match counts. Default content.",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case insensitive search (rg -i). Default false.",
                },
                "context": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Lines of context before and after each match (rg -C). content mode only.",
                },
                "context_before": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Lines before each match (rg -B). Ignored if context is set.",
                },
                "context_after": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Lines after each match (rg -A). Ignored if context is set.",
                },
                "head_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        f"Max results to return after offset. Default {DEFAULT_HEAD_LIMIT}. "
                        "Pass 0 for the high safety cap (use sparingly)."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Skip first N results before applying head_limit. Default 0.",
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline matching (rg -U --multiline-dotall). Default false.",
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
        path: Optional[str] = None,
        include: Optional[str] = None,
        glob: Optional[str] = None,
        type: Optional[str] = None,
        output_mode: str = "content",
        case_insensitive: bool = False,
        context: Optional[int] = None,
        context_before: Optional[int] = None,
        context_after: Optional[int] = None,
        head_limit: int = DEFAULT_HEAD_LIMIT,
        offset: int = 0,
        multiline: bool = False,
    ) -> ToolResult:
        if not pattern:
            return ToolErrorResult("pattern is required")

        mode = (output_mode or "content").strip().lower()
        if mode not in ("content", "files_with_matches", "count"):
            return ToolErrorResult(f"invalid output_mode: {output_mode}")

        if head_limit is None:
            head_limit = DEFAULT_HEAD_LIMIT
        if int(head_limit) < 0:
            return ToolErrorResult("head_limit must be >= 0")
        if offset is None:
            offset = 0
        if int(offset) < 0:
            return ToolErrorResult("offset must be >= 0")

        try:
            search, single_file = _resolve_search_target(path, agent_ctx.workspace_path or "")
            if single_file is None and (not search.exists() or not search.is_dir()):
                return ToolErrorResult(f"grep failed: directory does not exist: {search}")
            if single_file is not None and not single_file.is_file():
                return ToolErrorResult(f"grep failed: file does not exist: {single_file}")
        except ValueError as e:
            return ToolErrorResult(f"grep failed: {e}")
        except Exception as e:
            return ToolErrorResult(f"grep failed: {e}")

        query = GrepQuery(
            pattern=pattern,
            include=(include or glob or None),
            file_type=type,
            case_insensitive=bool(case_insensitive),
            context=context,
            context_before=context_before,
            context_after=context_after,
            head_limit=int(head_limit),
            offset=int(offset),
            multiline=bool(multiline),
        )
        try:
            rx = query.compile_regex()
        except re.error as e:
            return ToolErrorResult(f"invalid regex: {e}")

        try:
            output = await _grep_execute(search, single_file, query, rx, mode, run_ctx)
            if output is None:
                return run_ctx.aborted_tool_result(self.name)
            return ToolSuccessResult(output)
        except Exception as e:
            return ToolErrorResult(f"grep failed: {e}")
