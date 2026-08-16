import logging
import os
import re
from typing import List,Optional,Set
import tree_sitter_cpp as tscpp
from tree_sitter import Language, Parser
from app.utils.common import normalize_path
from .base import LanguageAnalyzer
from ..model import FileInfo, FunctionInfo, ClassInfo, ClassType, FunctionType, Language as Lang

# 全局变量存储已加载的语言
LANGUAGES = {}

def get_language():
    """获取或初始化 C++ 语言解析器"""
    if 'cpp' not in LANGUAGES:
        try:
            cpp_lang = Language(tscpp.language())
            parser = Parser()
            parser.language = cpp_lang
            LANGUAGES['cpp'] = parser
        except Exception as e:
            logging.error(f"Error loading C++ language: {str(e)}")
            return None
    return LANGUAGES['cpp']

class CppAnalyzer(LanguageAnalyzer):
    def __init__(self, base_path: str, file_path: str):
        """初始化C++分析器"""
        super().__init__(base_path, file_path)        
        # 获取解析器
        self.parser = get_language()
    
    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        """分析C++文件"""    
        if self.parser is None:
            logging.error(f"CPP parser is not initialized")
            return None

        try:
            content = source if source is not None else self._read_source_file()

            tree = self.parser.parse(bytes(content, 'utf8'))
            if not tree:
                return None
                
            functions = []
            classes = []
            
            # 遍历语法树
            cursor = tree.walk()
            
            async def visit_node(node):
                if node.type == 'function_definition':
                    func_name = self._get_function_name(node)
                    if not func_name.startswith('_'):
                        func_node = await self._create_function_node(node, content)
                        if func_node:
                            functions.append(func_node)
                elif node.type in ('class_specifier', 'struct_specifier', 'class_definition'):
                    class_node = await self._create_class_node(node, content)
                    if class_node:
                        classes.append(class_node)
                
                for child in node.children:
                    await visit_node(child)
            
            await visit_node(tree.root_node)

            imports = self.get_imports(content)
            cur_file_rel_path = normalize_path(os.path.relpath(self.file_path, self.base_path))
            dep_paths = self._dependent_files_from_includes(imports, cur_file_rel_path)
            
            return FileInfo(
                name=os.path.basename(self.file_path),
                file_path=cur_file_rel_path,
                language=Lang.CPP,
                functions=functions,
                classes=classes,
                imports=imports,
                dependent_files=dep_paths,
            )
        except Exception as e:
            logging.error(f"Error analyzing C++ file {self.file_path}: {str(e)}")
            return None
    
    def get_imports(self, content: str) -> List[str]:
        """获取C++文件的导入依赖"""
        imports: List[str] = []
        include_pattern = r'#include\s*[<"]([^>"]+)[>"]'
        for match in re.finditer(include_pattern, content):
            imports.append(match.group(1))
        return list(dict.fromkeys(imports))

    def _dependent_files_from_includes(self, includes: List[str], cur_file_rel_path: str) -> List[str]:
        """把 C++ 的 include 映射到本仓库内被依赖文件（低档：文件级静态依赖）。"""
        dependent_files: Set[str] = set()
        cur_dir = os.path.dirname(self.file_path)

        def add_if_inside_repo(abs_path: str) -> bool:
            if not os.path.isfile(abs_path):
                return False
            rel = normalize_path(os.path.relpath(abs_path, self.base_path))
            if rel.startswith("../") or rel == cur_file_rel_path:
                return False
            dependent_files.add(rel)
            return True

        for inc in includes:
            if not inc:
                continue
            inc = inc.strip().replace("\\", "/")
            if not inc:
                continue

            # 本地优先：当前文件目录 -> 仓库根目录
            local_candidate = os.path.normpath(os.path.join(cur_dir, inc.replace("/", os.sep)))
            if add_if_inside_repo(local_candidate):
                continue

            repo_candidate = os.path.normpath(os.path.join(self.base_path, inc.replace("/", os.sep)))
            add_if_inside_repo(repo_candidate)

        return sorted(dependent_files)
        
    def _get_function_name(self, node) -> str:
        """获取函数名（identifier 在 function_declarator 下）。"""
        for child in node.children:
            if child.type == "identifier":
                return child.text.decode("utf8")
            if child.type in ("function_declarator", "qualified_identifier", "field_identifier"):
                if child.type == "field_identifier":
                    return child.text.decode("utf8")
                for sub in child.children:
                    if sub.type in ("identifier", "field_identifier", "destructor_name", "operator_name"):
                        return sub.text.decode("utf8")
                    if sub.type in ("qualified_identifier", "function_declarator"):
                        nested = self._get_function_name(sub if sub.type == "function_declarator" else child)
                        if nested:
                            return nested
                        if sub.type == "qualified_identifier":
                            for q in sub.children:
                                if q.type in ("identifier", "type_identifier"):
                                    return q.text.decode("utf8")
        return ""
        
    async def _create_function_node(self, node, content: str) -> Optional[FunctionInfo]:
        """创建函数节点"""
        func_name = self._get_function_name(node)
        if not func_name:
            return None

        source_code = content[node.start_byte : node.end_byte]
        signature = f"{func_name}()"
        for child in node.children:
            if child.type == "function_declarator":
                signature = content[child.start_byte : child.end_byte].strip()
                break

        return FunctionInfo(
            name=func_name,
            full_name=func_name,
            signature=signature,
            type=FunctionType.FUNCTION.value,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            source_code=source_code,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            params=[],
            param_types=[],
            returns=[],
            return_types=[],
            docstring="",
        )

    def _get_class_name(self, node) -> str:
        for child in node.children:
            if child.type in ("type_identifier", "identifier"):
                return child.text.decode("utf8")
        return ""

    async def _get_class_methods(self, node, content: str) -> List[FunctionInfo]:
        methods: List[FunctionInfo] = []
        for child in node.children:
            if child.type != "field_declaration_list":
                continue
            for item in child.children:
                if item.type == "function_definition":
                    fn = await self._create_function_node(item, content)
                    if fn:
                        fn.type = FunctionType.METHOD.value
                        fn.class_name = self._get_class_name(node)
                        fn.full_name = f"{fn.class_name}.{fn.name}" if fn.class_name else fn.name
                        methods.append(fn)
                elif item.type == "declaration" or item.type == "field_declaration":
                    # 仅声明的方法：从 function_declarator 取名
                    name = self._get_function_name(item)
                    if not name or name.startswith("_"):
                        continue
                    source_code = content[item.start_byte : item.end_byte]
                    cls_name = self._get_class_name(node)
                    methods.append(
                        FunctionInfo(
                            name=name,
                            full_name=f"{cls_name}.{name}" if cls_name else name,
                            signature=source_code.strip().split("{", 1)[0].strip(),
                            type=FunctionType.METHOD.value,
                            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
                            source_code=source_code,
                            start_line=item.start_point[0] + 1,
                            end_line=item.end_point[0] + 1,
                            params=[],
                            param_types=[],
                            returns=[],
                            return_types=[],
                            docstring="",
                            class_name=cls_name,
                        )
                    )
        return methods

    async def _create_class_node(self, node, content: str) -> Optional[ClassInfo]:
        """创建类节点"""
        class_name = self._get_class_name(node)
        if not class_name:
            return None

        source_code = content[node.start_byte : node.end_byte]
        return ClassInfo(
            name=class_name,
            full_name=class_name,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            node_type=ClassType.CLASS.value,
            source_code=source_code,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            methods=await self._get_class_methods(node, content),
            attributes=[],
            docstring="",
        )
