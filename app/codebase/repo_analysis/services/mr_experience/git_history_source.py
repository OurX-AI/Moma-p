from __future__ import annotations
import logging
import subprocess
from datetime import datetime
from typing import List, Optional
from .models import FileChange, GitHistoryEntry


class GitHistorySource:
    """从本地 git 仓采集合入/提交历史（merge 优先，否则普通 commit）。"""

    @staticmethod
    def collect(
        repo_path: str,
        *,
        since: Optional[str] = None,
        after_sha: Optional[str] = None,
        limit: int = 50,
    ) -> List[GitHistoryEntry]:
        merges = GitHistorySource._list_commits(
            repo_path, merges_only=True, since=since, after_sha=after_sha, limit=limit
        )
        if merges:
            return [GitHistorySource._enrich(repo_path, e) for e in merges]
        normals = GitHistorySource._list_commits(
            repo_path, merges_only=False, since=since, after_sha=after_sha, limit=limit
        )
        return [GitHistorySource._enrich(repo_path, e) for e in normals]

    @staticmethod
    def has_new_entries(repo_path: str, after_sha: Optional[str] = None) -> bool:
        """相对 after_sha 检测是否还有未采集的 merge（无 merge 时看普通 commit）。"""
        merges = GitHistorySource._list_commits(
            repo_path, merges_only=True, since=None, limit=1
        )
        merges_only = bool(merges)
        if after_sha is None:
            entries = GitHistorySource._list_commits(
                repo_path, merges_only=merges_only, since=None, limit=1
            )
            return bool(entries)
        return (
            GitHistorySource._count_commits_after(
                repo_path, after_sha=after_sha, merges_only=merges_only
            )
            > 0
        )

    @staticmethod
    def _count_commits_after(
        repo_path: str, *, after_sha: str, merges_only: bool
    ) -> int:
        args = ["rev-list"]
        if merges_only:
            args.append("--merges")
        else:
            args.append("--no-merges")
        args.extend([f"{after_sha}..HEAD", "--count"])
        try:
            out = GitHistorySource._run_git(repo_path, args).strip()
            return int(out or 0)
        except (RuntimeError, ValueError):
            return 0

    @staticmethod
    def _run_git(repo_path: str, args: List[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"git {' '.join(args)} 失败: {err}")
        return completed.stdout or ""

    @staticmethod
    def _list_commits(
        repo_path: str,
        *,
        merges_only: bool,
        since: Optional[str],
        after_sha: Optional[str] = None,
        limit: int = 50,
    ) -> List[GitHistoryEntry]:
        args = ["log", f"-n{max(1, limit)}", "--format=%H%x09%ct%x09%s%x09%P"]
        if merges_only:
            args.append("--merges")
        else:
            args.append("--no-merges")
        if after_sha:
            args.append(f"{after_sha}..HEAD")
        elif since:
            args.append(f"--since={since}")
        try:
            out = GitHistorySource._run_git(repo_path, args)
        except RuntimeError as e:
            logging.warning("读取 git log 失败 path=%s error=%s", repo_path, e)
            return []
        entries: List[GitHistoryEntry] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            sha, ts, subject = parts[0], parts[1], parts[2]
            parents = parts[3].split() if len(parts) > 3 and parts[3] else []
            is_merge = len(parents) > 1
            if merges_only and not is_merge:
                continue
            if not merges_only and is_merge:
                continue
            committed_at = None
            try:
                committed_at = datetime.fromtimestamp(int(ts))
            except (TypeError, ValueError):
                pass
            entries.append(
                GitHistoryEntry(
                    commit_sha=sha,
                    message=(subject or "").strip(),
                    committed_at=committed_at,
                    is_merge=is_merge,
                )
            )
        return entries

    @staticmethod
    def _enrich(repo_path: str, entry: GitHistoryEntry) -> GitHistoryEntry:
        status_map: dict[str, str] = {}
        numstat_map: dict[str, tuple[int, int]] = {}
        try:
            name_out = GitHistorySource._run_git(
                repo_path,
                ["show", "--name-status", "--format=", "--no-renames", entry.commit_sha],
            )
            for line in name_out.splitlines():
                cols = line.split("\t")
                if len(cols) < 2:
                    continue
                st = (cols[0] or "M")[:1].upper()
                path = cols[-1].replace("\\", "/")
                if path:
                    status_map[path] = st
            num_out = GitHistorySource._run_git(
                repo_path,
                ["show", "--numstat", "--format=", "--no-renames", entry.commit_sha],
            )
            for line in num_out.splitlines():
                cols = line.split("\t")
                if len(cols) != 3:
                    continue
                add_s, del_s, path = cols[0], cols[1], cols[2].replace("\\", "/")
                if not path:
                    continue
                try:
                    add_n = 0 if add_s == "-" else int(add_s)
                    del_n = 0 if del_s == "-" else int(del_s)
                except ValueError:
                    continue
                numstat_map[path] = (add_n, del_n)
        except RuntimeError as e:
            logging.warning("读取 commit diff 失败 sha=%s error=%s", entry.commit_sha, e)
            return entry

        files: List[FileChange] = []
        for path in sorted(set(status_map) | set(numstat_map)):
            add_n, del_n = numstat_map.get(path, (0, 0))
            files.append(
                FileChange(
                    path=path,
                    status=status_map.get(path, "M"),
                    additions=add_n,
                    deletions=del_n,
                )
            )
        entry.files = files
        return entry
