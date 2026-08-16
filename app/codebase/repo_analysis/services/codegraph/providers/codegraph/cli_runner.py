"""开源 codegraph CLI 定位与执行封装。"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional


class CodeGraphCliError(RuntimeError):
    """开源 codegraph CLI 不可用或命令失败。"""


class CodeGraphCliRunner:
    """定位 codegraph 可执行文件并执行子命令。"""

    @staticmethod
    def find_cli() -> Optional[str]:
        found = shutil.which("codegraph")
        if found:
            lower = found.lower()
            if lower.endswith(".ps1"):
                cmd = found[:-4] + ".cmd"
                if os.path.isfile(cmd):
                    return cmd
            return found
        npm = shutil.which("npm")
        if not npm:
            return None
        try:
            completed = subprocess.run(
                [npm, "root", "-g"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        root = (completed.stdout or "").strip()
        if not root:
            return None
        npm_bin = os.path.dirname(root)
        candidates = [
            os.path.join(npm_bin, "codegraph.cmd"),
            os.path.join(npm_bin, "codegraph"),
            os.path.join(npm_bin, "codegraph.ps1"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    @classmethod
    def install_cli(cls) -> str:
        npm = shutil.which("npm")
        if not npm:
            raise CodeGraphCliError(
                "未找到 codegraph，且系统无 npm，无法自动安装。"
                "请先安装 Node.js/npm，或手动安装：npm i -g @colbymchenry/codegraph"
            )
        logging.info("正在安装开源 CodeGraph：npm i -g @colbymchenry/codegraph")
        completed = subprocess.run(
            [npm, "i", "-g", "@colbymchenry/codegraph"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise CodeGraphCliError(f"自动安装 CodeGraph 失败: {err}")
        path = cls.find_cli()
        if not path:
            raise CodeGraphCliError(
                "CodeGraph 安装后仍未在 PATH 中找到 codegraph 命令，请新开终端或检查 PATH"
            )
        return path

    @classmethod
    def ensure_cli(cls) -> str:
        path = cls.find_cli()
        if path:
            return path
        return cls.install_cli()

    @classmethod
    def run(
        cls,
        args: List[str],
        *,
        cwd: Optional[str] = None,
        project_path: Optional[str] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        cli = cls.ensure_cli()
        cmd = [cli, *args]
        if project_path:
            cmd.extend(["-p", project_path])
        logging.debug("执行 codegraph: %s cwd=%s", " ".join(cmd), cwd or project_path)
        completed = subprocess.run(
            cmd,
            cwd=cwd or project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise CodeGraphCliError(f"codegraph {' '.join(args)} 失败: {err}")
        return completed

    @classmethod
    def run_json(
        cls,
        args: List[str],
        *,
        project_path: str,
    ) -> Any:
        completed = cls.run([*args, "-j"], project_path=project_path, check=True)
        text = (completed.stdout or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CodeGraphCliError(f"codegraph JSON 解析失败: {exc}; raw={text[:500]}") from exc

    @classmethod
    def run_text(
        cls,
        args: List[str],
        *,
        project_path: str,
    ) -> str:
        completed = cls.run(args, project_path=project_path, check=True)
        return completed.stdout or ""
