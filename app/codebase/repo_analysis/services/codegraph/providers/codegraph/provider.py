"""开源 codegraph CLI Provider：生成图谱 + 查询适配。"""
from __future__ import annotations
import logging
import os
from typing import Any, Dict, List, Optional
from ...base import (
    CodeGraphGeneratorBase,
    CodeGraphProvider,
    CodeGraphSearchBase,
)
from ...model import QueryResponse
from .cli_runner import (
    CodeGraphCliError,
    CodeGraphCliRunner,
)
from .node_parser import NodeOutputParser
from ...graph_result_normalizer import GraphResultNormalizer


def find_codegraph_cli() -> Optional[str]:
    return CodeGraphCliRunner.find_cli()


def install_codegraph_cli() -> str:
    return CodeGraphCliRunner.install_cli()


def ensure_codegraph_cli() -> str:
    return CodeGraphCliRunner.ensure_cli()


class _CliGenerator(CodeGraphGeneratorBase):
    def __init__(self, repo_id: str, repo_name: str, repo_local_path: str, cli: str):
        self._repo_id = repo_id
        self._repo_local_path = repo_local_path
        self._cli = cli

    def close(self) -> None:
        return None

    async def delete_repo_graph(self) -> None:
        logging.info("codegraph 暂不支持 delete_repo_graph，已跳过 repo_id=%s", self._repo_id)

    async def delete_file_graph(self, rel_file_path: str) -> None:
        logging.info(
            "codegraph 暂不支持 delete_file_graph，已跳过 repo_id=%s file=%s",
            self._repo_id,
            rel_file_path,
        )

    async def generate_graph(self, clean_stale: bool = False):
        if not self._repo_local_path or not os.path.isdir(self._repo_local_path):
            raise CodeGraphCliError(f"仓库路径不可用: {self._repo_local_path}")
        logging.info("执行开源 CodeGraph init (cwd=%s)", self._repo_local_path)
        CodeGraphCliRunner.run(["init"], cwd=self._repo_local_path, check=True)
        return None

    async def refresh_graph(self) -> None:
        if not self._repo_local_path or not os.path.isdir(self._repo_local_path):
            return
        logging.info("执行开源 CodeGraph sync (cwd=%s)", self._repo_local_path)
        try:
            CodeGraphCliRunner.run(["sync"], cwd=self._repo_local_path, check=True)
        except CodeGraphCliError:
            logging.warning("CodeGraph sync 失败，回退 init cwd=%s", self._repo_local_path)
            await self.generate_graph(clean_stale=False)


class _CliSearch(CodeGraphSearchBase):
    def __init__(self, cli: str):
        self._cli = cli
        self._path_cache: Dict[str, str] = {}

    def close(self) -> None:
        return None

    async def _resolve_repo_path(self, repo_id: str) -> str:
        cached = self._path_cache.get(repo_id)
        if cached:
            return cached
        from app.infrastructure.database import get_db_session
        from ......repo_mgmt.services.repo_resolver import RepoResolver

        async with get_db_session() as db:
            repo = await RepoResolver.get_by_id(db, repo_id)
        if not repo or not repo.local_path:
            raise CodeGraphCliError(f"无法根据 repo_id={repo_id} 解析本地路径")
        path = RepoResolver.normalize_repo_path(repo.local_path)
        if not os.path.isdir(path):
            raise CodeGraphCliError(f"仓库本地路径不存在: {path}")
        self._path_cache[repo_id] = path
        return path

    def _node_trail_text(self, project_path: str, file_path: str) -> str:
        rel = NodeOutputParser.normalize_rel_path(file_path)
        base = os.path.basename(rel)
        return CodeGraphCliRunner.run_text(
            ["node", base, "-f", rel, "--symbols-only"],
            project_path=project_path,
        )

    def _node_header_text(self, project_path: str, file_path: str) -> str:
        rel = NodeOutputParser.normalize_rel_path(file_path)
        return CodeGraphCliRunner.run_text(
            ["node", "-f", rel, "--symbols-only"],
            project_path=project_path,
        )

    @staticmethod
    def _symbol_hits(payload: Any, key: str) -> List[Dict[str, Any]]:
        items = []
        if isinstance(payload, dict):
            raw = payload.get(key) or []
        else:
            raw = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            items.append(
                {
                    "name": it.get("name"),
                    "kind": it.get("kind"),
                    "file_path": NodeOutputParser.normalize_rel_path(str(it.get("filePath") or "")),
                    "start_line": it.get("startLine"),
                }
            )
        return items

    async def query_dependents_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        warn = GraphResultNormalizer.unsupported_file_message(file_path)
        try:
            project_path = await self._resolve_repo_path(repo_id)
            trail = self._node_trail_text(project_path, file_path)
            header = self._node_header_text(project_path, file_path)
            merged = f"{trail}\n{header}"
            dependents, _ = NodeOutputParser.parse_file_relations(merged, file_path)
            cleaned = GraphResultNormalizer.clean_paths(dependents, exclude=file_path)
            msg = warn or ("" if cleaned else "未解析到 dependents（可能索引不足或该文件无入边）")
            return QueryResponse(
                result=True,
                content={"dependents": cleaned, "warning": warn},
                message=msg,
            )
        except Exception as exc:
            return QueryResponse(result=False, content={}, message=str(exc))

    async def query_dependented_of_file(self, repo_id: str, file_path: str) -> QueryResponse:
        warn = GraphResultNormalizer.unsupported_file_message(file_path)
        try:
            project_path = await self._resolve_repo_path(repo_id)
            trail = self._node_trail_text(project_path, file_path)
            _, dependencies = NodeOutputParser.parse_file_relations(trail, file_path)
            cleaned = GraphResultNormalizer.clean_paths(dependencies, exclude=file_path)
            msg = warn or ("" if cleaned else "未解析到 dependencies（可能索引不足或该文件无出边）")
            return QueryResponse(
                result=True,
                content={"dependented": cleaned, "warning": warn},
                message=msg,
            )
        except Exception as exc:
            return QueryResponse(result=False, content={}, message=str(exc))

    async def query_file_summary(self, repo_id: str, file_paths: List[str]) -> QueryResponse:
        try:
            project_path = await self._resolve_repo_path(repo_id)
            files_summary: Dict[str, object] = {}
            warnings: List[str] = []
            for file_path in file_paths:
                rel = NodeOutputParser.normalize_rel_path(file_path)
                warn = GraphResultNormalizer.unsupported_file_message(rel)
                if warn:
                    warnings.append(warn)
                text = self._node_header_text(project_path, rel)
                files_summary[rel] = NodeOutputParser.parse_file_summary(text, rel)
            return QueryResponse(
                result=True,
                content={"files": files_summary, "warnings": warnings},
                message="; ".join(warnings) if warnings else "",
            )
        except Exception as exc:
            return QueryResponse(result=False, content={}, message=str(exc))

    async def query_callers_of_symbol(
        self,
        repo_id: str,
        symbol: str,
        limit: int = 20,
    ) -> QueryResponse:
        sym = (symbol or "").strip()
        if not sym:
            return QueryResponse(
                result=False,
                content={},
                message="symbol 为空，无法查询 callers；请提供符号名或改用 related",
            )
        try:
            project_path = await self._resolve_repo_path(repo_id)
            payload = CodeGraphCliRunner.run_json(
                ["callers", sym, "-l", str(max(limit * 2, limit))],
                project_path=project_path,
            )
            callers = GraphResultNormalizer.clean_symbol_hits(
                self._symbol_hits(payload, "callers"),
                limit=limit,
            )
            msg = (
                ""
                if callers
                else GraphResultNormalizer.missing_symbol_message(sym, "callers")
            )
            return QueryResponse(
                result=True,
                content={"symbol": sym, "callers": callers},
                message=msg,
            )
        except Exception as exc:
            if GraphResultNormalizer.is_symbol_not_found_error(exc):
                return QueryResponse(
                    result=True,
                    content={"symbol": sym, "callers": []},
                    message=GraphResultNormalizer.missing_symbol_message(sym, "callers"),
                )
            return QueryResponse(
                result=False,
                content={},
                message=f"callers 查询失败: {exc}；可降级 related/similar",
            )

    async def query_callees_of_symbol(
        self,
        repo_id: str,
        symbol: str,
        limit: int = 20,
    ) -> QueryResponse:
        sym = (symbol or "").strip()
        if not sym:
            return QueryResponse(
                result=False,
                content={},
                message="symbol 为空，无法查询 callees；请提供符号名或改用 related",
            )
        try:
            project_path = await self._resolve_repo_path(repo_id)
            payload = CodeGraphCliRunner.run_json(
                ["callees", sym, "-l", str(max(limit * 2, limit))],
                project_path=project_path,
            )
            callees = GraphResultNormalizer.clean_symbol_hits(
                self._symbol_hits(payload, "callees"),
                limit=limit,
            )
            msg = (
                ""
                if callees
                else GraphResultNormalizer.missing_symbol_message(sym, "callees")
            )
            return QueryResponse(
                result=True,
                content={"symbol": sym, "callees": callees},
                message=msg,
            )
        except Exception as exc:
            if GraphResultNormalizer.is_symbol_not_found_error(exc):
                return QueryResponse(
                    result=True,
                    content={"symbol": sym, "callees": []},
                    message=GraphResultNormalizer.missing_symbol_message(sym, "callees"),
                )
            return QueryResponse(
                result=False,
                content={},
                message=f"callees 查询失败: {exc}；可降级 related/similar",
            )


class CodeGraphCliProvider(CodeGraphProvider):
    """开源 codegraph CLI（默认）：检测/安装 codegraph 命令。"""

    name = "codegraph"

    def ensure_ready(self) -> None:
        from app.config.settings import settings
        if not settings.code_graph_enabled:
            return
        ensure_codegraph_cli()

    def create_generator(
        self,
        repo_id: str,
        repo_name: str,
        repo_local_path: str,
    ) -> CodeGraphGeneratorBase:
        cli = ensure_codegraph_cli()
        return _CliGenerator(repo_id, repo_name, repo_local_path, cli)

    def create_search(self) -> CodeGraphSearchBase:
        cli = ensure_codegraph_cli()
        return _CliSearch(cli)
