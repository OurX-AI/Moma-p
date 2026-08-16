"""仓库变更探测：优先 git，失败则由调用方回退 mtime。"""
from __future__ import annotations
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Set
from app.config.settings import settings


class ScanChangeDetector:
    """探测本地仓是否需要增量重扫；持久化上次扫描时的 git HEAD。"""

    @classmethod
    def fingerprint_path(cls, repo_id: str) -> Path:
        root = Path(settings.runtime_data_dir) / "scan_fingerprints"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{repo_id}.json"

    @classmethod
    def load_last_git_head(cls, repo_id: str) -> Optional[str]:
        path = cls.fingerprint_path(repo_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        head = str(data.get("git_head") or "").strip()
        return head or None

    @classmethod
    def save_git_head(cls, repo_id: str, git_head: Optional[str]) -> None:
        path = cls.fingerprint_path(repo_id)
        payload = {"git_head": (git_head or "").strip() or None}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def is_git_repo(cls, repo_root: str) -> bool:
        return os.path.isdir(os.path.join(repo_root, ".git"))

    @classmethod
    def current_head(cls, repo_root: str) -> Optional[str]:
        try:
            out = cls._run_git(repo_root, ["rev-parse", "HEAD"]).strip()
        except RuntimeError:
            return None
        return out or None

    @classmethod
    def has_source_working_tree_changes(
        cls,
        repo_root: str,
        extensions: Set[str],
    ) -> bool:
        """porcelain 中是否出现关心的源码扩展（含未跟踪）。"""
        try:
            out = cls._run_git(
                repo_root,
                ["status", "--porcelain", "-u", "--", "."],
            )
        except RuntimeError as e:
            logging.debug("git status 失败 path=%s error=%s", repo_root, e)
            raise
        for line in out.splitlines():
            if not line or len(line) < 4:
                continue
            # XY<space>path 或 rename: XY orig -> new
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[-1].strip()
            path_part = path_part.strip('"')
            ext = os.path.splitext(path_part)[1].lower()
            if ext in extensions:
                return True
        return False

    @classmethod
    def needs_rescan_by_git(
        cls,
        repo_id: str,
        repo_root: str,
        extensions: Set[str],
    ) -> Optional[bool]:
        """
        用 git 判断是否需要重扫。
        返回 None 表示无法用 git 判断，调用方应回退 mtime/count。
        """
        if not cls.is_git_repo(repo_root):
            return None
        try:
            if cls.has_source_working_tree_changes(repo_root, extensions):
                return True
            head = cls.current_head(repo_root)
            if not head:
                return None
            last = cls.load_last_git_head(repo_id)
            if last is None:
                # 从未记录过 HEAD：交给调用方结合 last_scan_finished_at 决策
                return None
            return head != last
        except RuntimeError:
            return None

    @staticmethod
    def _run_git(repo_path: str, args: list[str]) -> str:
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
