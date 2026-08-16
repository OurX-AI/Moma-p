import logging
import os
import re
from typing import List,Optional,Set
from tree_sitter import Language, Parser
from app.utils.common import normalize_path
from .base import LanguageAnalyzer
from ..model import FileInfo, FunctionInfo, ClassInfo, FunctionType, ClassType, Language as Lang

# 全局变量存储已加载的语言
LANGUAGES = {}

def get_language():
    """获取或初始化 Go 语言解析器"""
    if 'go' not in LANGUAGES:
        try:
            # 导入 Go 语言定义
            import tree_sitter_go
            from tree_sitter import Language, Parser
            
            # 直接使用 PyCapsule 对象初始化 Language
            go_lang = Language(tree_sitter_go.language())
            
            # 创建解析器并设置语言
            parser = Parser()
            parser.language = go_lang
            LANGUAGES['go'] = parser
            
            logging.info("Successfully initialized Go language parser")
            
        except Exception as e:
            logging.error(f"Error loading Go language: {str(e)}")
            return None
    return LANGUAGES['go']

class GoAnalyzer(LanguageAnalyzer):
    def __init__(self, base_path: str, file_path: str):
        """初始化Go分析器"""
        super().__init__(base_path, file_path)
        
        # 获取解析器
        self.parser = get_language()
        if self.parser is None:
            logging.error("Failed to initialize Go parser")
            raise RuntimeError("Failed to initialize Go parser. Please ensure tree-sitter-go is properly installed.")
        self._go_module_path: Optional[str] = None
    
    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        """分析Go文件"""
        if self.parser is None:
            logging.error("Go parser is not initialized")
            return None
            
        try:
            content = source if source is not None else self._read_source_file()
            
            tree = self.parser.parse(bytes(content, 'utf8'))
            if not tree:
                logging.error(f"Failed to parse Go file: {self.file_path}")
                return None
                
            functions = []
            structs = []
            
            # 遍历语法树
            cursor = tree.walk()           
            async def visit_node(node):
                if node.type == 'function_declaration':
                    func_name = self._get_function_name(node)
                    if not func_name.startswith('_'):
                        func_node = await self._create_function_node(node, content)
                        if func_node:
                            functions.append(func_node)
                elif node.type == 'method_declaration':
                    method_name = self._get_method_name(node)
                    if not method_name.startswith('_'):
                        method_node = await self._create_method_node(node, content)
                        if method_node:
                            functions.append(method_node)
                elif node.type == 'type_declaration':
                    # 检查是否是结构体定义
                    for child in node.children:
                        if child.type == 'type_spec':
                            struct_node = await self._create_struct_node(child, content)
                            if struct_node:
                                structs.append(struct_node)
                
                for child in node.children:
                    await visit_node(child)
            
            await visit_node(tree.root_node)

            imports = self.get_imports(content, tree)
            cur_file_rel_path = normalize_path(os.path.relpath(self.file_path, self.base_path))
            dep_paths = self._dependent_files_from_imports(imports, cur_file_rel_path)

            return FileInfo(
                name=os.path.basename(self.file_path),
                file_path=cur_file_rel_path,
                language=Lang.GO,
                functions=functions,
                classes=structs,
                imports=imports,
                dependent_files=dep_paths,
            )
        
        except Exception as e:
            logging.error(f"Error analyzing Go file {self.file_path}: {str(e)}")
            return None
    
    def get_imports(self, content: str, tree) -> List[str]:
        """获取Go文件的导入依赖
        
        Args:
            content: 文件内容
            tree: AST语法树
            
        Returns:
            List[str]: 导入的包路径列表
        """
        if self.parser is None:
            return []
            
        try:
            imports = []
            def visit_imports(node):
                if node.type == 'import_declaration':
                    # 遍历 import_declaration 的子节点
                    for child in node.children:
                        # 处理 import_spec_list
                        if child.type == 'import_spec_list':
                            for spec in child.children:
                                if spec.type == 'import_spec':
                                    import_path = spec.text.decode('utf8').strip().strip('"')
                                    if import_path and not import_path.startswith('_'):
                                        imports.append(import_path)
                        # 处理单个 import_spec（单行导入的情况）
                        elif child.type == 'import_spec':
                            import_path = child.text.decode('utf8').strip().strip('"')
                            if import_path and not import_path.startswith('_'):
                                imports.append(import_path)
                for child in node.children:
                    visit_imports(child)
            
            visit_imports(tree.root_node)
            return list(dict.fromkeys(imports))
            
        except Exception as e:
            logging.error(f"Error getting imports for Go file {self.file_path}: {str(e)}")
            return []

    def _get_go_module_path(self) -> str:
        if self._go_module_path is not None:
            return self._go_module_path
        mod_path = os.path.join(self.base_path, "go.mod")
        try:
            with open(mod_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s.startswith("module "):
                        self._go_module_path = s[len("module "):].strip()
                        return self._go_module_path
        except OSError:
            pass
        self._go_module_path = ""
        return ""

    def _dependent_files_from_imports(self, imports: List[str], cur_file_rel_path: str) -> List[str]:
        """把 Go import 映射到本仓库内被依赖文件（FileInfo.dependent_files）。

        当前策略（第1档/低档）：只按 import 包路径做“文件级静态依赖”推导。
        - 仅解析本仓库内模块路径（go.mod module）下的 import
        - 对包目录取同层所有 .go（排除 *_test.go）作为依赖
        """
        dependent_files: Set[str] = set()
        module_path = self._get_go_module_path()
        if not module_path:
            return []

        for imp in imports:
            if not imp:
                continue
            imp = imp.strip()
            if not imp:
                continue

            if imp == module_path:
                rel_dir = ""
            elif imp.startswith(module_path + "/"):
                rel_dir = imp[len(module_path) + 1:]
            else:
                continue

            abs_dir = os.path.join(self.base_path, rel_dir.replace("/", os.sep))
            if not os.path.isdir(abs_dir):
                continue

            try:
                for name in os.listdir(abs_dir):
                    if not name.endswith(".go"):
                        continue
                    if name.endswith("_test.go"):
                        continue
                    abs_path = os.path.join(abs_dir, name)
                    if not os.path.isfile(abs_path):
                        continue
                    rel = normalize_path(os.path.relpath(abs_path, self.base_path))
                    if rel and rel != cur_file_rel_path:
                        dependent_files.add(rel)
            except OSError:
                continue

        return sorted(dependent_files)
        
    def _get_function_name(self, node) -> str:
        """获取函数名"""
        for child in node.children:
            if child.type == 'identifier':
                return child.text.decode('utf8')
        return ''
        
    def _get_struct_name(self, node) -> str:
        """获取结构体名"""
        for child in node.children:
            if child.type == 'type_identifier':
                return child.text.decode('utf8')
        return ''
    
    def _get_method_name(self, node) -> str:
        """获取方法名"""
        for child in node.children:
            if child.type == 'field_identifier':  # 方法声明使用field_identifier
                return child.text.decode('utf8')
        return ''
    
    async def _create_function_node(self, node, content: str) -> Optional[FunctionInfo]:
        """创建函数节点"""
        func_name = self._get_function_name(node)
        if not func_name:
            return None
            
        source_code = content[node.start_byte:node.end_byte]

        # 生成完整路径（模块路径+函数名）
        root_node = node
        while root_node.parent is not None:
            root_node = root_node.parent
        package_name = self._get_package_name(root_node)
        full_name = f"{package_name}.{func_name}"
        
        return FunctionInfo(
            name=func_name,
            full_name=full_name,
            signature=self._get_function_signature(node, content),
            type=FunctionType.FUNCTION.value,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            source_code=source_code,
            start_line=node.start_point[0],
            end_line=node.end_point[0],
            params=self._get_function_params(node),
            param_types=self._get_param_types(node),
            returns=self._get_function_returns(node),
            return_types=self._get_return_types(node),
            docstring=self._get_comment(node, content)
        )

    async def _create_method_node(self, node, content: str) -> Optional[FunctionInfo]:
        """创建函数节点"""
        method_name = self._get_method_name(node)
        if not method_name:
            return None
            
        source_code = content[node.start_byte:node.end_byte]

        # 生成完整路径（模块路径+函数名）
        root_node = node
        while root_node.parent is not None:
            root_node = root_node.parent
        package_name = self._get_package_name(root_node)
        full_name = f"{package_name}.{method_name}"
        
        return FunctionInfo(
            name=method_name,
            full_name=full_name,
            signature=self._get_function_signature(node, content),
            type=FunctionType.METHOD.value,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            source_code=source_code,
            start_line=node.start_point[0],
            end_line=node.end_point[0],
            params=self._get_function_params(node),
            param_types=self._get_param_types(node),
            returns=self._get_function_returns(node),
            return_types=self._get_return_types(node),
            docstring=self._get_comment(node, content)
        )
        
    async def _create_struct_node(self, node, content: str) -> Optional[ClassInfo]:
        """创建结构体或接口节点
        
        Args:
            node: AST节点
            content: 文件内容
            
        Returns:
            ClassNode: 结构体或接口节点
        """
        struct_name = self._get_struct_name(node)
        if not struct_name:
            return None
            
        source_code = content[node.start_byte:node.end_byte]
        
        # 判断是结构体还是接口
        node_type = ClassType.STRUCT
        for child in node.children:
            if child.type == 'interface_type':
                node_type = ClassType.INTERFACE
                break
        
        if node_type == ClassType.INTERFACE:
            # 处理接口方法
            methods = await self._get_interface_methods(node, content)
            attributes = []  # 接口没有属性字段
        else:
            # 处理结构体方法和字段
            methods = await self._get_struct_methods(content, struct_name, node)
            attributes = self._get_struct_fields(node)
        
        # 构建完整的类名（包含包名）
        root_node = node
        while root_node.parent is not None:
            root_node = root_node.parent
        package_name = self._get_package_name(root_node)
        full_name = f"{package_name}.{struct_name}" if package_name else struct_name
        
        return ClassInfo(
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            name=struct_name,
            full_name=full_name,
            node_type=node_type.value,
            source_code=source_code,
            start_line=node.start_point[0],
            end_line=node.end_point[0],
            methods=methods,
            attributes=attributes,
            docstring=self._get_comment(node, content)
        )
        
    def _get_package_name(self, root_node) -> str:
        """获取包名"""
        for child in root_node.children:
            if child.type == 'package_clause':
                for pkg_child in child.children:
                    if pkg_child.type == 'package_identifier':
                        return pkg_child.text.decode('utf8')
        return ""
        
    def _get_function_signature(self, node, content: str) -> str:
        """获取函数签名 - 只包含类型，不包含参数名"""
        func_name = self._get_function_name(node)
        param_types = self._get_param_types(node)
        return_types = self._get_function_returns(node)
        
        param_signature = ", ".join(param_types) if param_types else ""
        return_type_str = return_types[0] if return_types else "void"
        return f"{func_name}({param_signature}) -> {return_type_str}"
        
    def _get_function_params(self, node) -> List[str]:
        """获取函数参数列表"""
        params = []
        for child in node.children:
            if child.type == 'parameter_list':
                for param in child.children:
                    if param.type == 'parameter_declaration':
                        for p in param.children:
                            if p.type == 'identifier':
                                params.append(p.text.decode('utf8'))
        return params
        
    def _get_param_types(self, node) -> List[str]:
        """获取参数类型列表
        
        支持以下类型：
        - 基本类型：type_identifier
        - 指针类型：pointer_type
        - 限定类型：qualified_type
        """
        types = []
        for child in node.children:
            if child.type == 'parameter_list':
                for param in child.children:
                    if param.type == 'parameter_declaration':
                        type_str = self._extract_type(param)
                        if type_str:
                            types.append(type_str)
        return types
        
    def _extract_type(self, node) -> str:
        """提取类型信息
        
        处理类型节点，支持：
        - 基本类型：type_identifier
        - 指针类型：pointer_type
        - 限定类型：qualified_type
        - 切片类型：slice_type
        - 数组类型：array_type
        - 映射类型：map_type
        - 通道类型：channel_type
        - 函数类型：function_type
        - 接口类型：interface_type
        - 结构体类型：struct_type
        """
        for child in node.children:
            if child.type in ('type_identifier', 'slice_type', 'pointer_type', 
                            'array_type', 'map_type', 'channel_type', 
                            'function_type', 'interface_type', 'struct_type'):
                return child.text.decode('utf8')
            elif child.type == 'qualified_type':
                # 处理限定类型（如包名限定的类型）
                package = ""
                type_name = ""
                for part in child.children:
                    if part.type == 'package_identifier':
                        package = part.text.decode('utf8')
                    elif part.type == 'type_identifier':
                        type_name = part.text.decode('utf8')
                return f"{package}.{type_name}" if package else type_name
        return ""
        
    def _get_function_returns(self, node) -> List[str]:
        """获取函数返回值列表"""
        returns = []
        for child in node.children:
            if child.type == 'result':
                for r in child.children:
                    if r.type == 'type_identifier':
                        returns.append(r.text.decode('utf8'))
                    elif r.type == 'pointer_type':
                        returns.append(r.text.decode('utf8'))
                    elif r.type == 'qualified_type':
                        returns.append(r.text.decode('utf8'))
        return returns
        
    def _get_return_types(self, node) -> List[str]:
        """获取返回值类型列表"""
        return self._get_function_returns(node)
        
    def _get_comment(self, node, content: str) -> str:
        """获取注释"""
        # 查找节点前的注释
        start = node.start_byte
        while start > 0 and content[start-1].isspace():
            start -= 1
        if start > 2 and content[start-2:start] == '//':
            # 单行注释
            comment_start = start - 2
            while comment_start > 0 and content[comment_start-1] != '\n':
                comment_start -= 1
            return content[comment_start:start].strip()
        return ''
        
    def _get_struct_fields(self, node) -> List[str]:
        """获取结构体字段列表
        
        返回格式：[字段名:类型]
        例如：["ID:int", "Name:string", "Data:*bytes.Buffer"]
        """
        fields = []
        for child in node.children:
            if child.type == 'struct_type':
                for struct_child in child.children:
                    if struct_child.type == 'field_declaration_list':
                        for field in struct_child.children:
                            if field.type == 'field_declaration':
                                field_name = ""
                                field_type = ""
                                for f in field.children:
                                    if f.type == 'field_identifier':
                                        field_name = f.text.decode('utf8')
                                    elif f.type in ('type_identifier', 'pointer_type', 'qualified_type'):
                                        field_type = f.text.decode('utf8')
                                if field_name and field_type:
                                    fields.append(f"{field_name}:{field_type}")
        return fields
        

    # 未调测验证    
    async def _get_struct_methods(self, content: str, struct_name: str, node=None) -> List[FunctionInfo]:
        """获取结构体方法列表
        
        Args:
            content: 文件内容
            struct_name: 结构体名称
            node: AST节点
            
        Returns:
            List[FunctionInfo]: 结构体方法列表
        """
        if self.parser is None or node is None:
            return []
            
        try:
            methods = []
            
            def visit_methods(node):
                if node.type == 'method_declaration':
                    # 检查接收者是否是目标结构体
                    receiver = node.child_by_field_name('receiver')
                    if receiver:
                        for child in receiver.children:
                            if child.type == 'parameter_declaration':
                                for param_child in child.children:
                                    # 检查接收者类型（支持指针和非指针类型）
                                    if param_child.type in ('type_identifier', 'pointer_type'):
                                        receiver_type = param_child.text.decode('utf8')
                                        # 移除指针前缀 *
                                        if receiver_type.startswith('*'):
                                            receiver_type = receiver_type[1:]
                                        if receiver_type == struct_name:
                                            return True
                return False
            
            async def process_node(node):
                if node.type == 'method_declaration' and visit_methods(node):
                    method_node = await self._create_function_node(node, content)
                    if method_node:
                        methods.append(method_node)
                
                for child in node.children:
                    await process_node(child)
                
            if hasattr(node, 'root_node'):
                await process_node(node.root_node)
            
            return methods
            
        except Exception as e:
            logging.error(f"Error getting methods for struct {struct_name}: {str(e)}")
            return []
        
    async def _create_interface_method_node(self, method_elem, content: str) -> Optional[FunctionInfo]:
        """创建接口方法节点
        
        Args:
            method_elem: 方法元素节点
            content: 文件内容
            
        Returns:
            Optional[FunctionNode]: 接口方法节点
        """
        try:
            method_name = method_elem.text.decode('utf8')
            if not method_name:
                return None
                
            source_code = content[method_elem.start_byte:method_elem.end_byte]
            
            return FunctionInfo(
                name=method_name,
                full_name=method_name,  # 接口方法不需要包名前缀
                signature=self._get_function_signature(method_elem, content),
                type=FunctionType.METHOD.value,
                file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
                source_code=source_code,
                start_line=method_elem.start_point[0],
                end_line=method_elem.end_point[0],
                params=self._get_function_params(method_elem),
                param_types=self._get_param_types(method_elem),
                returns=self._get_function_returns(method_elem),
                return_types=self._get_return_types(method_elem),
                docstring=self._get_comment(method_elem, content)
            )
        except Exception as e:
            logging.error(f"Error creating interface method node: {str(e)}")
            return None
            
    async def _get_interface_methods(self, node, content: str) -> List[FunctionInfo]:
        """获取接口方法列表
        
        Args:
            node: AST节点
            content: 文件内容
            
        Returns:
            List[FunctionInfo]: 接口方法列表
        """
        methods = []
        try:
            for child in node.children:
                if child.type == 'interface_type':
                    for interface_child in child.children:
                        if interface_child.type == 'method_elem':
                            method_node = await self._create_interface_method_node(interface_child, content)
                            if method_node:
                                methods.append(method_node)
            return methods
        except Exception as e:
            logging.error(f"Error getting interface methods: {str(e)}")
            return [] 
