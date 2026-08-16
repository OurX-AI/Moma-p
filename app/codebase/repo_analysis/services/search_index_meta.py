from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy import select
from app.infrastructure.database import get_db_session
from ..models.analysis_status import RepoAnalysisTask


class SearchIndexMeta:
    """查询结果附带的只读索引新鲜度（不触发同步）。"""

    @staticmethod
    async def for_repo(repo_id: str) -> Dict[str, Any]:
        async with get_db_session() as db:
            task = await db.scalar(select(RepoAnalysisTask).where(RepoAnalysisTask.repo_id == repo_id))
        if not task:
            return {
                "last_scan_finished_at": None,
                "index_age_seconds": None,
                "scan_status": None,
            }
        finished = task.last_scan_finished_at
        age: Optional[float] = None
        if finished is not None:
            age = max(0.0, (datetime.now() - finished).total_seconds())
        return {
            "last_scan_finished_at": finished.isoformat() if finished else None,
            "index_age_seconds": age,
            "scan_status": task.scan_status,
        }
