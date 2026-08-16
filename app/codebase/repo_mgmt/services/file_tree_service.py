import json
import os
from enum import Enum
from typing import Dict, List, Optional
from app.utils.common import normalize_path

class PathInfo:
    def __init__(self, path: str = "", name: str = "", is_directory: bool = False, size: int = 0):
        self.path = path
        self.name = name
        self.is_directory = is_directory
        self.size = size

# 定义FileNode的类型枚举
class FileTreeNodeType(Enum):
    File = "F"
    Directory = "D"

class FileTreeNode:
    def __init__(self, name: str = "", node_type: FileTreeNodeType = FileTreeNodeType.Directory):
        self.name = name
        self.type = node_type
        self.children: Dict[str, 'FileTreeNode'] = {}  #key是name，value是Node
    
    @property
    def is_file(self) -> bool:
        """是否为叶子节点（文件）"""
        return self.type == FileTreeNodeType.File
    
    @property
    def is_directory(self) -> bool:
        """是否为目录节点"""
        return self.type == FileTreeNodeType.Directory


# 本地目录下的目录+文件树处理服务
class FileTreeService:
    """基于本地仓库文件的目录操作和文件操作"""    

    @staticmethod
    def build_tree(path_infos: List[PathInfo], base_path: str) -> FileTreeNode:
        """根据指定路径列表构建文件树"""

        root = FileTreeNode(name="/", node_type=FileTreeNodeType.Directory)
        
        for path_info in path_infos:
            # 计算相对路径
            relative_path = path_info.path.replace(base_path, "").lstrip('\\/')
            
            # 过滤.开头的文件
            if relative_path.startswith("."):
                continue
            
            # 分割路径
            parts = [part for part in normalize_path(relative_path).split("/") if part]
            
            # 从根节点开始构建路径
            current_node = root
            
            # 样例：
            # 输入路径: "src/components/Header.tsx" (文件)
            #
            # FillTree树结构：
            # root("/")
            # └── src(D)
            #     └── components(D)
            #         └── Header.tsx(F)            
            for i, part in enumerate(parts):
                is_last_part = i == len(parts) - 1
                
                if part not in current_node.children:
                    current_node.children[part] = FileTreeNode(
                        name=part,
                        node_type=FileTreeNodeType.File if (is_last_part and not path_info.is_directory) else FileTreeNodeType.Directory
                    )
                
                current_node = current_node.children[part]
        
        return root
    
    @staticmethod
    def get_all_paths(node: FileTreeNode, current_path: str = "") -> List[str]:
        """获取文件树的所有完整路径，用于验证结构完整性
        
        样例：
        public(D)
        public/favicon.ico(F)
        public/images(D)
        public/images/logo.png(F)
        README.md(F)
        src(D)
        src/components(D)
        src/components/Footer.tsx(F)
        src/components/Header.tsx(F)
        src/utils(D)
        src/utils/helper.js(F)
        """
        all_paths = []
        
        # 遍历子节点，不排序（与C#版本一致）
        for child_name, child_node in node.children.items():
            # 构建子节点路径
            child_path = child_name if not current_path else f"{current_path}/{child_name}"
            
            # 添加当前路径（目录也要记录）
            node_type = "D" if child_node.is_directory else "F"
            all_paths.append(f"{child_path}({node_type})")
            
            # 如果是目录且有子节点，递归获取子路径
            if child_node.is_directory and child_node.children:
                all_paths.extend(FileTreeService.get_all_paths(child_node, child_path))
        
        return all_paths
    
    @staticmethod
    def to_compact_string(node: FileTreeNode, indent: int = 0) -> str:
        """将文件树转换为紧凑的字符串表示
            level 结构层级(空格个数)

            处理后的格式：
            /
            public/D
            favicon.ico/F
            images/D
                logo.png/F
            README.md/F
            src/D
            components/D
                Footer.tsx/F
                Header.tsx/F
            utils/D
                helper.js/F
        """
        result = []
        indent_str = "  " * indent
        
        # 根节点特殊处理
        if indent == 0:
            result.append("/")
        
        # 按照目录优先，然后按名称排序的方式遍历子节点
        sorted_children = sorted(node.children.items(), 
                               key=lambda x: (x[1].is_file, x[0]))
        
        for child_name, child_node in sorted_children:
            # 输出当前节点信息
            node_type = "D" if child_node.is_directory else "F"
            result.append(f"{indent_str}{child_name}/{node_type}")
            
            # 如果是目录，递归处理子目录
            if child_node.is_directory:
                child_content = FileTreeService.to_compact_string(child_node, indent + 1)
                if child_content.strip():
                    result.append(child_content)
        
        return "\n".join(result)
    
    @staticmethod
    def to_compact_json(node: FileTreeNode) -> str:
        """将文件树转换为紧凑的JSON格式
        样例：
        {"public":{"favicon.ico":"F","images":{"logo.png":"F"}},"README.md":"F","src":{"components":{"Footer.tsx":"F","Header.tsx":"F"},"utils":{"helper.js":"F"}}}
        """
        def serialize_node_compact(node: FileTreeNode):
            # 如果当前节点是文件，直接返回 "F"
            if node.is_file:
                return "F"
            
            result = {}
            
            # 遍历子节点（注意这里忽略掉了/根节点）
            for name, child in node.children.items():
                result[name] = serialize_node_compact(child)
            
            return result
        
        return json.dumps(serialize_node_compact(node), ensure_ascii=False, separators=(',', ':'))
    

    @staticmethod
    def to_path_list(node: FileTreeNode, current_path: str = "") -> str:
        """将文件树转换为路径列表
        样例：
        public/
        public/favicon.ico
        public/images/
        public/images/logo.png
        README.md
        src/
        src/components/
        src/components/Footer.tsx
        src/components/Header.tsx
        src/utils/
        src/utils/helper.js
        """
        paths = []
        
        # 遍历子节点，不排序（与C#版本一致）
        for child_name, child_node in node.children.items():
            # 构建子节点路径
            child_path = child_name if not current_path else f"{current_path}/{child_name}"
            
            if child_node.is_file:
                paths.append(child_path)
            else:
                # 如果目录只有一个子节点，可以压缩路径
                if len(child_node.children) == 1:
                    sub_paths = FileTreeService.to_path_list(child_node, child_path).split('\n')
                    if sub_paths == ['']:  # 处理空字符串分割结果
                        sub_paths = []
                    paths.extend(sub_paths)
                else:
                    paths.append(f"{child_path}/")
                    sub_paths = FileTreeService.to_path_list(child_node, child_path).split('\n')
                    if sub_paths == ['']:  # 处理空字符串分割结果
                        sub_paths = []
                    paths.extend(sub_paths)
        
        return "\n".join(paths)

    @staticmethod
    def to_unix_tree(node: FileTreeNode, prefix: str = "", is_last: bool = True) -> str:
        """将文件树转换为Unix树形格式
        样例：
        .
        ├── public/
        │   ├── favicon.ico
        │   └── images/
        │       └── logo.png
        ├── README.md
        └── src/
            ├── components/
            │   ├── Footer.tsx
            │   └── Header.tsx
            └── utils/
                └── helper.js
        """
        result = []
        
        # 根节点处理
        if not prefix:
            result.append(".")
            sorted_children = sorted(node.children.items(), 
                                   key=lambda x: (x[1].is_file, x[0]))
            
            for child_name, child_node in sorted_children:
                is_last_child = child_name == sorted_children[-1][0]
                result.append(FileTreeService._to_unix_tree_recursive(child_node, "", is_last_child, child_name))
        else:
            # 非根节点处理
            result.append(FileTreeService._to_unix_tree_recursive(node, prefix, is_last, node.name))
        
        return "\n".join(result)
    
    @staticmethod
    def _to_unix_tree_recursive(node: FileTreeNode, prefix: str, is_last: bool, node_name: str) -> str:
        """Unix风格树形结构的递归实现"""
        result = []
        
        # 当前节点的连接符
        connector = "└── " if is_last else "├── "
        
        # 输出当前节点
        node_suffix = "/" if node.is_directory else ""
        result.append(f"{prefix}{connector}{node_name}{node_suffix}")
        
        # 如果是目录且有子节点，递归处理子节点
        if node.is_directory and node.children:
            child_prefix = prefix + ("    " if is_last else "│   ")
            sorted_children = sorted(node.children.items(), 
                                   key=lambda x: (x[1].is_file, x[0]))
            
            for i, (child_name, child_node) in enumerate(sorted_children):
                is_last_child = i == len(sorted_children) - 1
                result.append(FileTreeService._to_unix_tree_recursive(child_node, child_prefix, is_last_child, child_name))
        
        return "\n".join(result)