"""Lib 分析编排：扫描/状态复用 repo_analysis。"""
from typing import Dict
from ...repo_analysis.services.analysis_service import AnalysisService


class LibAnalysisService:
    """kind=lib 的分析入口（委托扫描与清理；清理已含 API 向量删除）。"""

    @staticmethod
    async def start_scan(repo_id: str) -> Dict[str, object]:
        return await AnalysisService.start_scan(repo_id=repo_id)

    @staticmethod
    async def get_summary(repo_id: str) -> Dict[str, object]:
        return await AnalysisService.get_summary(repo_id)

    @staticmethod
    async def stop_scan(repo_id: str) -> Dict[str, object]:
        return await AnalysisService.stop_scan(repo_id)

    @staticmethod
    async def delete_repo_analysis_data(repo_id: str) -> None:
        await AnalysisService.delete_repo_analysis_data(repo_id)
