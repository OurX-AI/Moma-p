import os
from typing import List, Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..models.git_repo_mgmt import GitRepository
from app.utils.common import normalize_path


class RepoResolver:
    """按 repo_id 或 repo_path 解析仓库。"""

    @staticmethod
    def normalize_repo_path(repo_path: str) -> str:
        return normalize_path(os.path.abspath(os.path.normpath(repo_path))).rstrip("/")

    @staticmethod
    def _path_key(repo_path: str) -> str:
        return RepoResolver.normalize_repo_path(repo_path).lower()

    @staticmethod
    def is_path_prefix_match(query_path: str, repo_local_path: str) -> bool:
        """query 精确等于仓路径，或为仓路径的上级前缀（仓在 query 之下）。"""
        q = RepoResolver._path_key(query_path)
        r = RepoResolver._path_key(repo_local_path)
        if not q or not r:
            return False
        return r == q or r.startswith(q + "/")

    @staticmethod
    async def get_by_id(db: AsyncSession, repo_id: str) -> Optional[GitRepository]:
        result = await db.execute(select(GitRepository).where(GitRepository.id == repo_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_path(db: AsyncSession, repo_path: str) -> Optional[GitRepository]:
        normalized = RepoResolver.normalize_repo_path(repo_path)
        result = await db.execute(select(GitRepository))
        for repo in result.scalars().all():
            if not repo.local_path:
                continue
            repo_local = RepoResolver.normalize_repo_path(repo.local_path)
            if repo_local == normalized:
                return repo
        return None

    @staticmethod
    async def list_by_path_prefix(
        db: AsyncSession,
        query_path: str,
        *,
        user_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[GitRepository]:
        """精确匹配该 path 的仓，以及登记路径以该 path 为前缀的子仓。"""
        result = await db.execute(select(GitRepository))
        matched: List[GitRepository] = []
        want_kind = (kind or "").strip().lower() or None
        for repo in result.scalars().all():
            if not repo.local_path:
                continue
            if user_id is not None and repo.user_id != user_id:
                continue
            if want_kind is not None:
                repo_kind = (getattr(repo, "kind", None) or "code").strip().lower()
                if repo_kind != want_kind:
                    continue
            if RepoResolver.is_path_prefix_match(query_path, repo.local_path):
                matched.append(repo)
        matched.sort(key=lambda r: RepoResolver._path_key(r.local_path or ""))
        return matched

    @staticmethod
    async def resolve(
        db: AsyncSession,
        repo_id: Optional[str] = None,
        repo_path: Optional[str] = None,
    ) -> Optional[GitRepository]:
        if repo_id:
            return await RepoResolver.get_by_id(db, repo_id)
        if repo_path:
            return await RepoResolver.get_by_path(db, repo_path)
        return None

    @staticmethod
    async def expand_search_paths(
        db: AsyncSession,
        paths: Sequence[str],
        *,
        user_id: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> List[GitRepository]:
        """
        展开检索目标仓：
        - 每个 path：精确仓 + 路径前缀下的子仓；
        - 多个 --path：并集去重；
        - kind 可选：仅保留 code / lib。
        """
        by_id: dict[str, GitRepository] = {}
        for raw in paths:
            s = str(raw or "").strip()
            if not s:
                continue
            for repo in await RepoResolver.list_by_path_prefix(
                db, s, user_id=user_id, kind=kind
            ):
                by_id[repo.id] = repo
        return sorted(by_id.values(), key=lambda r: RepoResolver._path_key(r.local_path or ""))
