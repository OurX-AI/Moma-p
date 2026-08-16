import logging
import os
from typing import List,Optional,Set
import javalang
from app.utils.common import normalize_path
from .base import LanguageAnalyzer
from ..model import FileInfo,FunctionInfo,ClassInfo,ClassType,FunctionType,Language as Lang


class JavaAnalyzer(LanguageAnalyzer):
    def __init__(self, base_path: str, file_path: str):
        """初始化Java分析器"""
        super().__init__(base_path, file_path)
    
    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        """分析Java文件"""
        try:
            content = source if source is not None else self._read_source_file()
            
            tree = javalang.parse.parse(content)
            if not tree:
                raise Exception(f"Failed to parse Java file: {self.file_path}")
                
            functions = []
            classes = []
            cur_file_rel_path = normalize_path(os.path.relpath(self.file_path, self.base_path))
            
            # 分析Java类和方法
            for path, node in tree.filter(javalang.tree.ClassDeclaration):
                class_node = await self._create_class_node(node, content)
                if class_node:
                    classes.append(class_node)

            imports = self.get_imports(content)
            dep_paths = self._dependent_files_from_imports(imports, cur_file_rel_path)
            
            return FileInfo(
                name=os.path.basename(self.file_path),
                file_path=cur_file_rel_path,
                language=Lang.JAVA,
                functions=functions,
                classes=classes,
                imports=imports,
                dependent_files=dep_paths,
            )

        except Exception as e:
            logging.error(f"Error analyzing Java file {self.file_path}: {str(e)}")
            return None
        
    def get_imports(self, content: str) -> List[str]:
        """获取Java文件的导入依赖"""
        imports: List[str] = []
        try:
            tree = javalang.parse.parse(content)
            for path, node in tree.filter(javalang.tree.Import):
                dotted = node.path
                if getattr(node, "wildcard", False):
                    dotted = f"{dotted}.*"
                if dotted:
                    imports.append(dotted)
        except:
            pass
        return list(dict.fromkeys(imports))

    def _dependent_files_from_imports(self, imports: List[str], cur_file_rel_path: str) -> List[str]:
        """把 Java import 映射到本仓库内被依赖文件（FileInfo.dependent_files）。

        当前策略（第1档/低档）：只按 import 语句做“文件级静态依赖”推导，不做符号级、也不追踪同包未 import 的类型。
        """

        dependent_files: Set[str] = set()

        def add_if_exists(dotted_prefix: str) -> bool:
            if not dotted_prefix:
                return False
            rel_base = dotted_prefix.replace(".", os.sep)
            abs_path = os.path.join(self.base_path, f"{rel_base}.java")
            if os.path.isfile(abs_path):
                rel = normalize_path(os.path.relpath(abs_path, self.base_path))
                if rel and rel != cur_file_rel_path:
                    dependent_files.add(rel)
                    return True
            return False

        def add_from_dotted_import(dotted: str) -> None:
            # 允许处理“类名.内部类名/成员名”的情况：
            # com.foo.Outer.Inner -> 先找 Inner.java，不存在则退回 Outer.java
            parts = dotted.split(".")
            # 最多向上回退 2 级，避免把无关包名当成类名
            max_cut = min(2, len(parts) - 1)
            for cut in range(0, max_cut + 1):
                prefix_parts = parts[:-cut] if cut > 0 else parts
                prefix = ".".join(prefix_parts)
                found = add_if_exists(prefix)
                # add_if_exists 已经做了存在性判断；若找到就直接结束
                if found:
                    break

        def add_from_package_wildcard(package_dotted: str) -> None:
            # 只有“包级通配符 import x.y.*;”能直接扩展为多个 .java 文件。
            # 对于“static 通配符 import static x.y.Z.*;”，通常 dotted_prefix 能直接落到 Z.java。
            # 因此先尝试当作类名解析，再不行才按包目录枚举同层 .java。
            # 1) 优先尝试把 prefix 当作类名（兼容 static wildcard）
            before_count = len(dependent_files)
            add_from_dotted_import(package_dotted)
            if len(dependent_files) > before_count:
                return

            abs_dir = os.path.join(self.base_path, package_dotted.replace(".", os.sep))
            if not os.path.isdir(abs_dir):
                return

            for name in os.listdir(abs_dir):
                if not name.endswith(".java"):
                    continue
                abs_path = os.path.join(abs_dir, name)
                if not os.path.isfile(abs_path):
                    continue
                rel = normalize_path(os.path.relpath(abs_path, self.base_path))
                if rel and rel != cur_file_rel_path:
                    dependent_files.add(rel)

        for imp in imports:
            if not imp:
                continue
            imp = imp.strip()
            if not imp:
                continue

            # 通配符：import x.y.*; 或 import static x.y.Z.*;
            if imp.endswith(".*"):
                pkg_or_class = imp[: -len(".*")]
                add_from_package_wildcard(pkg_or_class)
                continue
            if imp.endswith("*"):
                pkg_or_class = imp[: -len("*")]
                add_from_package_wildcard(pkg_or_class)
                continue

            # 普通 import：import x.y.Z;
            # 以及 static import：import static x.y.Z.member;
            add_from_dotted_import(imp)

        return sorted(dependent_files)

    async def _create_class_node(self, node, content: str) -> Optional[ClassInfo]:
        """创建类节点"""
        methods = []
        for method in node.methods:
            if not method.name.startswith('_'):
                method_node = await self._create_method_node(method, content)
                if method_node:
                    methods.append(method_node)
        
        # 获取类的源代码
        lines = content.split('\n')
        start_line = node.position.line - 1 if node.position else 0
        # 计算结束行：找到最后一个方法的结束行，或者使用类的最后一个可见字符的行
        end_line = start_line
        if methods:
            end_line = max(m.end_line for m in methods if m.end_line) if methods else start_line
        else:
            # 如果没有方法，尝试找到类的结束大括号
            for i in range(start_line, len(lines)):
                if '}' in lines[i] and lines[i].strip().startswith('}'):
                    end_line = i + 1
                    break
        
        source_code = '\n'.join(lines[start_line:end_line]) if start_line < len(lines) else ""
        
        # 构建完整类名
        package_name = ""
        try:
            tree = javalang.parse.parse(content)
            for path, pkg_node in tree.filter(javalang.tree.PackageDeclaration):
                package_name = pkg_node.name
                break
        except:
            pass
        
        full_name = f"{package_name}.{node.name}" if package_name else node.name
        
        return ClassInfo(
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            name=node.name,
            full_name=full_name,
            node_type=ClassType.CLASS.value,
            source_code=source_code,
            start_line=start_line + 1,
            end_line=end_line,
            methods=methods,
            attributes=self._get_class_attributes(node),
            docstring=self._get_comment(node)
        )

    async def _create_method_node(self, node, content: str) -> Optional[FunctionInfo]:
        """创建方法节点"""
        lines = content.split('\n')
        start_line = node.position.line - 1 if node.position else 0
        # 估算结束行：从开始行查找结束大括号
        end_line = start_line
        brace_count = 0
        for i in range(start_line, min(start_line + 100, len(lines))):  # 限制搜索范围
            line = lines[i]
            brace_count += line.count('{') - line.count('}')
            if brace_count == 0 and i > start_line:
                end_line = i + 1
                break
        else:
            end_line = min(start_line + 10, len(lines))  # 如果找不到，使用默认值
        
        source_code = '\n'.join(lines[start_line:end_line]) if start_line < len(lines) else ""
        
        # 构建完整方法名
        package_name = ""
        class_name = ""
        try:
            tree = javalang.parse.parse(content)
            for path, pkg_node in tree.filter(javalang.tree.PackageDeclaration):
                package_name = pkg_node.name
                break
            for path, cls_node in tree.filter(javalang.tree.ClassDeclaration):
                if node in cls_node.methods:
                    class_name = cls_node.name
                    break
        except:
            pass
        
        full_name = f"{package_name}.{class_name}.{node.name}" if package_name and class_name else (f"{class_name}.{node.name}" if class_name else node.name)
        signature = self._get_method_signature(node)
        
        return FunctionInfo(
            name=node.name,
            full_name=full_name,
            signature=signature,
            type=FunctionType.METHOD.value,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            source_code=source_code,
            start_line=start_line + 1,
            end_line=end_line,
            params=self._get_method_params(node),
            param_types=self._get_param_types(node),
            returns=self._get_method_returns(node),
            return_types=self._get_return_types(node),
            docstring=self._get_comment(node)
        )
    
    def _get_method_signature(self, node) -> str:
        """获取方法签名 - 只包含类型，不包含参数名"""
        param_types = []
        for param in node.parameters:
            param_type = param.type.name if param.type else "Object"
            param_types.append(param_type)
        
        return_type = node.return_type.name if (node.return_type and hasattr(node.return_type, 'name')) else "void"
        param_signature = ", ".join(param_types)
        return f"{node.name}({param_signature}) -> {return_type}"
    
    def _get_method_params(self, node) -> List[str]:
        """获取方法参数名列表"""
        return [p.name for p in node.parameters]
    
    def _get_param_types(self, node) -> List[str]:
        """获取方法参数类型列表"""
        return [p.type.name if p.type else "Object" for p in node.parameters]
    
    def _get_method_returns(self, node) -> List[str]:
        """获取方法返回值列表"""
        if node.return_type:
            return [node.return_type.name if hasattr(node.return_type, 'name') else str(node.return_type)]
        return ["void"]
    
    def _get_return_types(self, node) -> List[str]:
        """获取方法返回类型列表"""
        if node.return_type:
            return [node.return_type.name if hasattr(node.return_type, 'name') else str(node.return_type)]
        return ["void"]
    
    def _get_class_attributes(self, node) -> List[str]:
        """获取类属性列表"""
        attributes = []
        for field in node.fields:
            for declarator in field.declarators:
                attributes.append(declarator.name)
        return attributes
    
    def _get_comment(self, node) -> Optional[str]:
        """获取注释"""
        if hasattr(node, 'documentation') and node.documentation:
            return node.documentation
        return None 