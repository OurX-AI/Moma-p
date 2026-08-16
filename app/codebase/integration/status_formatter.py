"""CodeBase 状态报告格式化器。

CLI `moma codebase status` 与 TUI `/codebase` 共用 `format_codebase_status`；
TUI 实时面板用 `format_codebase_panel_text` 输出精简多行文本（适配 welcome-box 右侧 ~40 列宽）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .facade import CodebaseFacade
from ..repo_analysis.models.analysis_status import RepoAnalysisStatus
from ..repo_analysis.services.analysis_service import AnalysisService
from ..repo_mgmt.services.repo_resolver import RepoResolver
from app.config.settings import settings
from app.infrastructure.database import get_db_session


def _fmt_ago(dt_value: Any) -> str:
    """datetime 或 ISO 字符串 -> '3s ago' / '2m ago' / '1h ago'；空值返回 '-'。"""
    if not dt_value:
        return "-"
    try:
        if isinstance(dt_value, str):
            dt = datetime.fromisoformat(dt_value)
        else:
            dt = dt_value
    except ValueError:
        return "-"
    delta = max(0, int((datetime.now() - dt).total_seconds()))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_time(dt_value: Any) -> str:
    """datetime 或 ISO 字符串 -> '12:34:05'；空值返回 '-'。"""
    if not dt_value:
        return "-"
    try:
        if isinstance(dt_value, str):
            dt = datetime.fromisoformat(dt_value)
        else:
            dt = dt_value
    except ValueError:
        return "-"
    return dt.strftime("%H:%M:%S")


def _short_path(path: str, max_len: int = 48) -> str:
    if not path:
        return "-"
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


async def _resolve_repo_id(workspace_path: str) -> Optional[str]:
    async with get_db_session() as db:
        repo = await RepoResolver.get_by_path(db, workspace_path)
        return repo.id if repo else None


async def format_codebase_status(workspace_path: str, user_id: str) -> str:
    """返回多行纯文本状态报告。CLI/TUI /codebase 共用。"""
    repo_id = await _resolve_repo_id(workspace_path)
    if not repo_id:
        return (
            "CodeBase 状态\n"
            f"- workspace: {_short_path(workspace_path)}\n"
            "- repo_id:   (未注册)\n"
            "- 提示:      尚未登记，启动 moma 后会自动扫描登记；或用 /codebase rescan 触发"
        )

    summary = await AnalysisService.get_summary(repo_id=repo_id)
    scan = await AnalysisService.get_scan_status(repo_id=repo_id)
    analysis = dict(summary or {})

    scan_status = str(scan.get("scan_status") or "unknown")
    last_error = scan.get("last_error")
    started = scan.get("last_scan_started_at")
    finished = scan.get("last_scan_finished_at")
    heartbeat = scan.get("scan_heartbeat_at")

    total = int(analysis.get("total") or 0)
    chunk_completed = int(analysis.get("chunk_completed") or 0)
    chunk_running = int(analysis.get("chunk_running") or 0)
    chunk_failed = int(analysis.get("chunk_failed") or 0)
    chunk_skipped = int(analysis.get("chunk_skipped") or 0)
    summary_completed = int(analysis.get("summary_completed") or 0)
    summary_running = int(analysis.get("summary_running") or 0)
    summary_failed = int(analysis.get("summary_failed") or 0)
    summary_skipped = int(analysis.get("summary_skipped") or 0)
    can_query = bool(analysis.get("searchable"))
    graph_enabled = bool(getattr(settings, "code_graph_enabled", False))
    index_age = analysis.get("index_age_seconds")

    scan_line = scan_status
    if scan_status == RepoAnalysisStatus.RUNNING.value:
        scan_line += f"  (started {_fmt_time(started)}, heartbeat {_fmt_ago(heartbeat)})"
    elif scan_status == RepoAnalysisStatus.COMPLETED.value:
        scan_line += f"  (finished {_fmt_ago(finished)})"
    elif scan_status == RepoAnalysisStatus.FAILED.value:
        scan_line += f"  (failed {_fmt_ago(finished)})"

    query_glyph = "✓ 可查询" if can_query else "✗ 不可查询"
    graph_line = "enabled" if graph_enabled else "disabled"
    if isinstance(index_age, (int, float)) and scan_status == RepoAnalysisStatus.COMPLETED.value:
        graph_line += f"  (index age {int(index_age)}s)"

    lines = [
        "CodeBase 状态",
        f"- workspace: {_short_path(workspace_path)}",
        f"- repo_id:   {repo_id}",
        f"- scan:      {scan_line}",
        (
            f"- chunk:     {chunk_completed} completed  ·  {chunk_running} running  ·  "
            f"{chunk_failed} failed  ·  {chunk_skipped} skipped"
        ),
        (
            f"- summary:   {summary_completed} completed  ·  {summary_running} running  ·  "
            f"{summary_failed} failed  ·  {summary_skipped} skipped"
        ),
        f"- graph:     {graph_line}",
        f"- query:     {query_glyph}",
    ]
    if last_error:
        lines.append(f"- last_err:  {last_error}")
    else:
        lines.append("- last_err:  (none)")
    return "\n".join(lines)


def format_codebase_panel_text(scan: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """TUI 实时面板精简文本（welcome-box 右侧 panel-body-dim max-height=2，故压成 2 行）。

    Args:
        scan: AnalysisService.get_scan_status 返回值
        summary: AnalysisService.get_summary 直接返回的 dict
    """
    total = int(summary.get("total") or 0)
    chunk_completed = int(summary.get("chunk_completed") or 0)
    chunk_running = int(summary.get("chunk_running") or 0)
    chunk_failed = int(summary.get("chunk_failed") or 0)
    summary_running = int(summary.get("summary_running") or 0)
    summary_failed = int(summary.get("summary_failed") or 0)

    scan_status = str(scan.get("scan_status") or "unknown")

    # 第 1 行：扫描状态 + 进度
    line1 = f"{scan_status} · {chunk_completed}/{total} chunk"
    # 第 2 行：明细 + 就绪度
    parts = [f"{chunk_running} run", f"{chunk_failed} fail"]
    if summary_running > 0 or summary_failed > 0:
        parts.append(f"sum {summary_running}r/{summary_failed}f")
    line2 = " · ".join(parts)
    if chunk_completed > 0 and chunk_running == 0 and chunk_failed == 0:
        line2 += " · ✓ ready"
    else:
        line2 += " · ✗"
    return f"{line1}\n{line2}"


def format_codebase_status_indicator(scan: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """底部 status-row 一行简短指示器（welcome-box 隐藏后仍可见）。"""
    total = int(summary.get("total") or 0)
    chunk_completed = int(summary.get("chunk_completed") or 0)
    scan_status = str(scan.get("scan_status") or "")
    if scan_status == RepoAnalysisStatus.RUNNING.value:
        return f"CB ⟳ {chunk_completed}/{total} chunk"
    return ""


async def format_experience_status(workspace_path: str) -> str:
    """返回 MR 经验提取状态报告。CLI `moma codebase experience` 与 TUI `/experience` 共用。"""
    from sqlalchemy import select, func
    from ..repo_analysis.models.experience_status import (
        RepoExperienceTask,
        MrExperienceItem,
    )

    repo_id = await _resolve_repo_id(workspace_path)
    if not repo_id:
        return (
            "MR 经验提取状态\n"
            f"- workspace: {_short_path(workspace_path)}\n"
            "- repo_id:   (未注册)\n"
            "- 提示:      尚未登记，启动 moma 后会自动扫描登记"
        )

    async with get_db_session() as db:
        task = await db.scalar(
            select(RepoExperienceTask).where(RepoExperienceTask.repo_id == repo_id)
        )
        if not task:
            return (
                "MR 经验提取状态\n"
                f"- workspace: {_short_path(workspace_path)}\n"
                f"- repo_id:   {repo_id}\n"
                "- 状态:      尚未启动（等待增量扫描触发）"
            )

        # 按 status 分组统计条目数
        stats = await db.execute(
            select(MrExperienceItem.status, func.count(MrExperienceItem.id))
            .where(MrExperienceItem.repo_id == repo_id)
            .group_by(MrExperienceItem.status)
        )
        status_counts = {row[0]: row[1] for row in stats}

    lines = [
        "MR 经验提取状态",
        f"- workspace:  {_short_path(workspace_path)}",
        f"- repo_id:    {repo_id}",
        f"- job_status: {task.job_status}",
        f"- last_error: {task.last_error or '(none)'}",
        f"- last_started:  {_fmt_ago(task.last_started_at)}",
        f"- last_finished: {_fmt_ago(task.last_finished_at)}",
        f"- total_items:   {task.total_items}",
        f"- ready_items:   {task.ready_items}",
        f"- failed_items:  {task.failed_items}",
        f"- last_sha:      {(task.last_collected_commit_sha or '-')[:12]}{'...' if task.last_collected_commit_sha and len(task.last_collected_commit_sha) > 12 else ''}",
        f"- last_collected_at: {_fmt_ago(task.last_collected_committed_at)}",
        "",
        "条目统计:",
    ]
    for status in ["pending", "running", "ready", "skipped", "failed"]:
        count = status_counts.get(status, 0)
        lines.append(f"  {status}: {count}")

    return "\n".join(lines)
