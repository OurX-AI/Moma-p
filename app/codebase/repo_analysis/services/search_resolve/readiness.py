from __future__ import annotations
from typing import Any, Dict
from ..analysis_service import AnalysisService
from .errors import ResolveIndexNotReady


class ResolveReadiness:
    """resolve 前只读就绪检查：无可搜索文件时阻断，避免空结果被当成「没找到」。"""

    @classmethod
    async def ensure_searchable(cls, repo_id: str) -> Dict[str, Any]:
        summary = await AnalysisService.get_summary(repo_id)
        if summary.get("searchable"):
            return summary

        scan = await AnalysisService.get_scan_status(repo_id)
        scan_status = scan.get("scan_status")
        hint = (
            "请先执行: mcb setup --path <仓> 或 mcb analyze --path <仓>；"
            "可用 mcb analyze status --path <仓> 查看进度。"
        )
        if scan_status == "running" or summary.get("in_memory_scan"):
            message = "索引仍在构建中，尚无可搜索文件"
        elif summary.get("chunk_completed", 0) == 0:
            message = "仓库尚未完成分析，尚无可搜索文件"
        else:
            message = "索引未就绪：无可搜索文件"
        raise ResolveIndexNotReady(
            message,
            details={
                "repo_id": repo_id,
                "chunk_completed": summary.get("chunk_completed", 0),
                "scan_status": scan_status,
                "hint": hint,
            },
        )
