import logging
import os
import re
from typing import List, Optional
from app.utils.common import normalize_path
from .file_tree_service import FileTreeService, PathInfo


class LocalRepoService:
    """基于本地仓库文件的目录操作和文件操作"""

    @staticmethod
    async def get_readme_file(path: str) -> str:
        """读取仓库的ReadMe文件"""
        readme_files = ["README.md", "README.rst", "README.txt", "README"]
        
        for file in readme_files:
            readme_path = os.path.join(path, file)
            if os.path.exists(readme_path):
                try:
                    with open(readme_path, 'r', encoding='utf-8') as f:
                        return f.read()
                except Exception as e:
                    logging.error(f"读取README文件失败 {readme_path}: {e}")
        
        return ""

    @staticmethod
    async def get_catalogue(path: str) -> str:
        """获取目录结构。包含文件夹和文件，仅输出相对路径，不包含图标；过滤相对路径以 '.' 开头的项"""
        info_list = await LocalRepoService.get_folders_and_files(path)
        
        lines: List[str] = []
        for info in info_list:
            relative_path = normalize_path(os.path.relpath(info.path, path))
            if relative_path.startswith("."):
                continue
            lines.append(relative_path)
        
        return "\n".join(lines)

    @staticmethod
    async def get_catalogue_optimized(path: str, format: str = "compact") -> str:
        """获取目录结构，可以指定Token压缩方式。包含文件夹和文件，仅输出相对路径，不包含图标；过滤相对路径以 '.' 开头的项"""
        info_list = await LocalRepoService.get_folders_and_files(path)
        tree = FileTreeService.build_tree(info_list, path)

        if format == "json":
            return FileTreeService.to_compact_json(tree)
        elif format == "unix":
            return FileTreeService.to_unix_tree(tree)
        elif format == "pathlist":
            return FileTreeService.to_path_list(tree)
        elif format == "compact":
            return FileTreeService.to_compact_string(tree)
        else:
            return ""
    
    @staticmethod
    async def get_folders_and_files(path: str) -> List[PathInfo]:
        """获取目录文件列表"""
        info_list = []
        ignore_files = await LocalRepoService._get_ignore_files(path)
        await LocalRepoService._scan_directory(path, info_list, ignore_files)
        return info_list

    @staticmethod
    async def _get_ignore_files(path: str) -> List[str]:
        """获取忽略文件列表"""
        ignore_files = []

        # 读取.gitignore文件
        gitignore_path = os.path.join(path, ".gitignore")
        if os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            ignore_files.append(line)
            except Exception as e:
                logging.error(f"读取.gitignore文件失败: {e}")
        
        return ignore_files


    # 扫描目录，获取目录结构
    @staticmethod
    async def _scan_directory(path: str, info_list: List[PathInfo], ignore_files: List[str]) -> None:
        """
         扫描目录
         忽略：1）大于1M的文件；2）.开头的目录 3）.gitignore中配置的文件
         返回格式：PathInfo列表。PathInfo包含路径、名称、是否为目录、大小
        """        
        try:
            # 遍历目录下的所有项目
            for item in os.listdir(path):
                # 绝对路径
                item_path = os.path.join(path, item)
                
                if os.path.isfile(item_path):
                    # 处理文件
                    # 检查是否应该忽略文件
                    should_ignore = False
                    for pattern in ignore_files:
                        if await LocalRepoService._should_ignore_pattern(pattern, item, False):
                            should_ignore = True
                            break
                    
                    if should_ignore:
                        continue
                    
                    # 过滤大于1M的文件
                    try:
                        size = os.path.getsize(item_path)
                        #if size >= 1024 * 1024:  # 1MB
                        #    continue
                        
                        info_list.append(PathInfo(
                            path=item_path,
                            name=item,
                            is_directory=False,
                            size=size
                        ))
                    except OSError:
                        continue                        
                elif os.path.isdir(item_path):
                    # 处理目录
                    # 过滤.开头的目录
                    if item.startswith("."):
                        continue
                    
                    # 检查是否应该忽略目录
                    should_ignore = False
                    for pattern in ignore_files:
                        if await LocalRepoService._should_ignore_pattern(pattern, item, True):
                            should_ignore = True
                            break
                    
                    if should_ignore:
                        continue
                    
                    # 记录目录本身
                    info_list.append(PathInfo(
                        path=item_path,
                        name=item,
                        is_directory=True,
                        size=0
                    ))
                    
                    # 递归扫描子目录
                    await LocalRepoService._scan_directory(item_path, info_list, ignore_files)
                        
        except PermissionError:
            logging.warning(f"没有权限访问目录: {path}")
        except Exception as e:
            logging.error(f"扫描目录失败 {path}: {e}")
    
    @staticmethod
    async def _should_ignore_pattern(pattern: str, name: str, is_directory: bool) -> bool:
        """检查是否应该忽略该模式"""
        try:
            # 跳过空行和注释
            if not pattern or pattern.startswith('#'):
                return False
            
            trimmed_pattern = pattern.strip()
            
            # 如果模式以/结尾，表示只匹配目录
            if trimmed_pattern.endswith('/'):
                if not is_directory:
                    return False
                trimmed_pattern = trimmed_pattern.rstrip('/')
            
            # 转换gitignore模式到正则表达式
            if '*' in trimmed_pattern:
                regex_pattern = "^" + re.escape(trimmed_pattern).replace("\\*", ".*") + "$"
                return bool(re.match(regex_pattern, name, re.IGNORECASE))
            else:
                # 精确匹配，大小写不敏感
                return name.lower() == trimmed_pattern.lower()
                
        except Exception:
            return False 