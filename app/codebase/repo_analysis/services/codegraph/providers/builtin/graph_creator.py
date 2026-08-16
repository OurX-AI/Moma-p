import json
import logging
import os
from typing import Dict, List
from app.config.settings import settings
from app.utils.common import local_now_iso, normalize_path
from ....codeast.ast_analyzer import FileAstAnalyzer, FolderAstAnalyzer
from ....codeast.model import Language
from .neo4j_service import Neo4jService

_MANIFEST_DIR = ".codegraph"
_MANIFEST_FILE = "manifest.json"


class CodeGraphGenerator:
    def __init__(self, repo_id: str, repo_name: str, repo_local_path: str):
        """初始化代码图谱生成器"""
        self.repo_id = repo_id
        self.repo_name = repo_name
        self.repo_local_path = repo_local_path
        self.db_client = None
        if settings.code_graph_enabled:
            self.db_client = Neo4jService(
                settings.neo4j_uri,
                settings.neo4j_user,
                settings.neo4j_password,
            )

    def close(self) -> None:
        if self.db_client:
            self.db_client.close()
            self.db_client = None

    async def delete_repo_graph(self) -> None:
        if not settings.code_graph_enabled or not self.db_client:
            return
        self.db_client.delete_repo_nodes(self.repo_id)

    async def delete_file_graph(self, rel_file_path: str) -> None:
        if not settings.code_graph_enabled or not self.db_client:
            return
        self.db_client.delete_file_nodes(self.repo_id, normalize_path(rel_file_path))

    async def generate_graph(self, clean_stale: bool = False):
        """生成或更新完整的代码知识图谱"""
        if not settings.code_graph_enabled or not self.db_client:
            logging.info("CODE_GRAPH_ENABLED=false或db_client为空，跳过图谱生成 repo_id=%s", self.repo_id)
            return

        start_time = local_now_iso()

        # 创建或更新项目节点
        self.db_client.save_project(
            self.repo_id,
            self.repo_name,
            self.repo_local_path
        )

        # 从项目根目录开始分析
        code_folder_analyzer = FolderAstAnalyzer(self.repo_local_path, self.repo_local_path)
        root_folder = await code_folder_analyzer.analyze_folder()

        # 保存文件夹结构
        self.db_client.save_folder_node(self.repo_id, root_folder)

        # 清理过期节点
        if clean_stale:
            self.db_client.delete_stale_nodes(self.repo_id, start_time)

        self._save_manifest(self._collect_files_from_tree(root_folder))
        return root_folder

    async def refresh_graph(self) -> None:
        """增量刷新：轻量扫描目录检测变更，仅对变更文件调用 update_files / delete_file_graph。"""
        if not settings.code_graph_enabled or not self.db_client:
            logging.info("CODE_GRAPH_ENABLED=false或db_client为空，跳过图谱刷新 repo_id=%s", self.repo_id)
            return

        if not self.repo_local_path or not os.path.isdir(self.repo_local_path):
            return

        new_manifest = self._scan_source_files()
        old_manifest = self._load_manifest()
        if not old_manifest:
            self._save_manifest(new_manifest)
            return

        old_set = set(old_manifest.keys())
        new_set = set(new_manifest.keys())

        added = new_set - old_set
        deleted = old_set - new_set
        changed = {
            p for p in (old_set & new_set)
            if new_manifest[p] != old_manifest[p]
        }

        to_update = [os.path.join(self.repo_local_path, p) for p in (added | changed)]
        if to_update:
            await self.update_files(to_update)

        for rel_path in deleted:
            try:
                await self.delete_file_graph(rel_path)
            except Exception as e:
                logging.warning("CodeGraph 删除文件图谱失败 repo_id=%s file=%s error=%s",
                                self.repo_id, rel_path, e)

        self._save_manifest(new_manifest)

    async def update_files(self, file_paths: List[str]):
        """增量更新指定文件，同步更新 manifest。"""
        if not settings.code_graph_enabled or not self.db_client:
            logging.info("CODE_GRAPH_ENABLED=false或db_client为空，跳过文件更新 repo_id=%s", self.repo_id)
            return
        
        manifest = self._load_manifest()
        updated = False
        for file_path in file_paths:
            if not os.path.isfile(file_path):
                continue

            # 转换为相对路径（与保存时一致）
            rel_path = normalize_path(os.path.relpath(file_path, self.repo_local_path))

            try:
                # 1. 删除文件相关的所有节点
                self.db_client.delete_file_nodes(self.repo_id, rel_path)

                # 2. 重新分析文件
                file_ast_analyzer = FileAstAnalyzer(self.repo_local_path, file_path)
                file_node = await file_ast_analyzer.analyze_file()

                # 3. 保存新的节点
                if file_node:
                    self.db_client.save_file_node(self.repo_id, file_node)
                    manifest[rel_path] = os.path.getmtime(file_path)
                    updated = True
            except Exception as e:
                # 记录错误但继续处理其他文件
                logging.error(f"Error updating file {file_path}: {str(e)}")
                continue

        if updated:
            self._save_manifest(manifest)

    async def update_folders(self, folder_paths: List[str]):
        """增量更新指定文件夹，同步更新 manifest。"""
        if not settings.code_graph_enabled or not self.db_client:
            return
        manifest = self._load_manifest()
        updated = False
        for folder_path in folder_paths:
            if not os.path.isdir(folder_path):
                continue

            # 转换为相对路径（与保存时一致）
            rel_path = normalize_path(os.path.relpath(folder_path, self.repo_local_path))

            try:
                # 1. 删除文件夹相关的所有节点
                self.db_client.delete_folder_nodes(self.repo_id, rel_path)

                # 2. 重新分析文件夹
                folder_ast_analyzer = FolderAstAnalyzer(self.repo_local_path, folder_path)
                folder_node = await folder_ast_analyzer.analyze_folder()

                # 3. 保存新的节点
                if folder_node:
                    self.db_client.save_folder_node(self.repo_id, folder_node)
                    # 将文件夹内文件同步到 manifest
                    folder_manifest = self._collect_files_from_tree(folder_node)
                    manifest.update(folder_manifest)
                    updated = True
            except Exception as e:
                # 记录错误但继续处理其他文件夹
                logging.error(f"Error updating folder {folder_path}: {str(e)}")
                continue

        if updated:
            self._save_manifest(manifest)

    # ---------- manifest ----------

    def _scan_source_files(self) -> Dict[str, float]:
        """轻量扫描：遍历目录获取 {rel_path: mtime}，不做 AST 分析。"""
        excluded = FolderAstAnalyzer.EXCLUDED_DIRS | {_MANIFEST_DIR}
        result: Dict[str, float] = {}
        # 复用同一个 FileAstAnalyzer 实例做语言检测
        lang_checker = FileAstAnalyzer(self.repo_local_path, "")
        for parent_root, dirs, files in os.walk(self.repo_local_path):
            dirs[:] = [d for d in dirs if d not in excluded]
            for name in files:
                abs_path = os.path.join(parent_root, name)
                lang_checker.file_path = abs_path
                if lang_checker._detect_language() == Language.UNKNOWN:
                    continue
                rel_path = os.path.relpath(abs_path, self.repo_local_path).replace("\\", "/")
                try:
                    result[rel_path] = os.path.getmtime(abs_path)
                except OSError:
                    continue
        return result

    def _collect_files_from_tree(self, folder) -> Dict[str, float]:
        """从 FolderInfo 树中递归提取 {rel_path: mtime}。"""
        result: Dict[str, float] = {}
        for f in folder.files:
            rel = normalize_path(f.file_path)
            abs_path = os.path.join(self.repo_local_path, rel)
            if os.path.isfile(abs_path):
                try:
                    result[rel] = os.path.getmtime(abs_path)
                except OSError:
                    result[rel] = 0.0
        for sub in folder.subfolders:
            result.update(self._collect_files_from_tree(sub))
        return result

    def _manifest_path(self) -> str:
        return os.path.join(self.repo_local_path, _MANIFEST_DIR, _MANIFEST_FILE)

    def _load_manifest(self) -> Dict[str, float]:
        path = self._manifest_path()
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_manifest(self, manifest: Dict[str, float]) -> None:
        path = self._manifest_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
