import ast
import logging
import os
from collections import deque
from typing import List,Optional,Set,Tuple
from app.utils.common import normalize_path
from .base import LanguageAnalyzer
from ..model import FileInfo, FunctionInfo, ClassInfo, CallInfo, ClassType, FunctionType, Language as Lang


class PythonAnalyzer(LanguageAnalyzer):
    def __init__(self, base_path: str, file_path: str):
        """初始化Python分析器"""
        super().__init__(base_path, file_path)

    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        """分析Python文件内容"""
        try:
            content = source if source is not None else self._read_source_file()
            
            tree = ast.parse(content)
            current_module = self._get_module_path()
            imports_map, import_module_prefixes = self._analyze_imports(tree, current_module)
            
            # 分别存储顶层函数和类
            functions = []
            classes = []
            
            # 记录类方法的集合，用于后续过滤顶层函数
            class_methods = set()
            
            # 第一次遍历：处理类定义和收集类方法
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    class_node, methods = await self.analyze_class(
                        node=node,
                        imports_map=imports_map,
                    )
                    classes.append(class_node)
                    class_methods.update(methods)  # 使用 update 而不是 add

            # 第二次遍历：只处理不在类中的函数
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node not in class_methods:
                        functions.append(
                            await self.analyze_function(
                                node=node,
                                imports_map=imports_map,
                                class_name=None,
                            )
                        )
            
            # 创建文件节点，统一使用正斜杠
            rel_self = normalize_path(os.path.relpath(self.file_path, self.base_path))
            dep_paths = self._dependent_files_from_import_module_prefixes(import_module_prefixes)
            return FileInfo(
                name=os.path.basename(self.file_path),
                file_path=rel_self,
                language=Lang.PYTHON,
                functions=functions,
                classes=classes,
                imports=list(imports_map.values()),
                dependent_files=dep_paths,
            )
            
        except Exception as e:
            logging.error(f"Error analyzing file {self.file_path}: {str(e)}")
            logging.error(f"Error type: {type(e)}")
            return None

    def _analyze_imports(
        self, tree: ast.AST, current_module: str
    ) -> Tuple[dict, Set[str]]:
        """获取Python文件的导入依赖，将相对导入转换为项目内的绝对路径（单次 ast.walk 同时收集解析 dependent_files 用的点路径集合）。
        Args:
            tree: 已解析的AST
            
        Returns:
            (imports_map, import_module_prefixes):
                imports_map — 导入映射 {local_name: full_path}
                import_module_prefixes — 由 import 语句归纳出的点路径集合（含包前缀与 `pkg.sym` 等），用于解析本仓库内 dependent_files
            
        Examples:
            对于文件 app/models/user.py:
            from ..utils import helper -> {helper: app.utils.helper}  / app/utils/helper.py
            from .base import Model -> {Model: app.models.base}  / app/models/base.py
            from app.config import settings -> {settings: app.config.settings}  / app/config/settings.py
        """
        imports_map: dict = {}
        import_module_prefixes: Set[str] = set()
        for node in ast.walk(tree):
            # ast.Import（样例 ⇄ 字段）：
            #   import os
            #     node.names：仅含本句的一个 alias；alias.name = 样例里的「os」；alias.asname = None。
            #   import numpy as np
            #     node.names：一个 alias；alias.name = 「numpy」；alias.asname = 「np」。
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports_map[alias.asname or alias.name] = alias.name
                    if alias.name:
                        import_module_prefixes.add(alias.name)
            # ast.ImportFrom（样例 ⇄ 字段）：
            #   from app.config import settings
            #     node.level = 0；node.module = 「app.config」；alias.name = 「settings」；alias.asname = None。
            #   from app.config import settings as cfg
            #     node.level = 0；alias.name = 「settings」；alias.asname = 「cfg」。
            #   from .base import Model（设 current_module = app.models.user）
            #     node.level = 1（一个点前缀）；node.module = 「base」；alias.name = 「Model」；full_module 由 current_module 去掉末段后与 node.module 拼接得 app.models.base。
            #   from ..utils import helper（同上 current_module）
            #     node.level = 2（两个点）；node.module = 「utils」；alias.name = 「helper」；full_module 对应 app.utils。
            #   full_module 再与 alias.name 拼成 imports 里的值。
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                level = node.level  # 相对导入的层级数
                
                if level > 0:
                    # 处理相对导入
                    current_parts = current_module.split('.')
                    if len(current_parts) < level:
                        # 相对导入超出了项目根目录，忽略
                        continue
                        
                    # 移除相应数量的路径部分
                    base_path = '.'.join(current_parts[:-level])
                    if module:
                        # 有模块名的情况：from ..utils import helper
                        full_module = f"{base_path}.{module}" if base_path else module
                    else:
                        # 没有模块名的情况：from .. import helper
                        full_module = base_path
                else:
                    # 绝对导入
                    full_module = module
                
                if full_module:
                    import_module_prefixes.add(full_module)
                # 处理导入的具体名称
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    if full_module:
                        imports_map[local_name] = f"{full_module}.{alias.name}"
                        if alias.name != "*":
                            import_module_prefixes.add(f"{full_module}.{alias.name}")
                    else:
                        imports_map[local_name] = alias.name
                        if alias.name != "*":
                            if current_module:
                                import_module_prefixes.add(f"{current_module}.{alias.name}")
                            elif alias.name:
                                import_module_prefixes.add(alias.name)
        return imports_map, import_module_prefixes

    async def analyze_function(
        self,
        node,
        imports_map: dict,
        class_name: str = None,
    ) -> FunctionInfo:
        """分析Python函数"""
        source_code = ast.unparse(node)
        
        # 分析参数
        params = self._get_function_params(node)
        param_types = []
        for arg in node.args.args:
            if arg.annotation:
                param_types.append(ast.unparse(arg.annotation))
            else:
                param_types.append("Any")
        
        # 分析返回值
        returns = self._get_function_returns(node)
        return_types = []
        if node.returns:
            return_types.append(ast.unparse(node.returns))
        else:
            return_types.append("Any")
        
        # 生成完整路径（模块路径+函数名）
        module_path = self._get_module_path()
        full_name = (
            f"{module_path}.{class_name}.{node.name}"
            if class_name
            else f"{module_path}.{node.name}"
        )
        
        # 生成函数签名（函数名+参数类型+返回类型）
        # 只包含参数类型，不包含参数名，避免参数名不同但类型相同的函数被认为是不同的签名
        param_signature = ", ".join(param_types)
        return_type_str = return_types[0] if return_types else "Any"
        signature = f"{node.name}({param_signature}) -> {return_type_str}"
        
        # 分析函数调用，传入导入映射和类名（如果是类方法）
        calls = await self._get_function_calls(
            node,
            imports_map,
            module_path,
            class_name,
        )
         
        return FunctionInfo(
            name=node.name,
            full_name=full_name,  # 包含完整路径的函数名
            signature=signature,  # 只包含函数签名信息
            type=FunctionType.FUNCTION.value,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            source_code=source_code,
            start_line=node.lineno,
            end_line=node.end_lineno,
            params=params,
            param_types=param_types,
            returns=returns,
            return_types=return_types,
            docstring=ast.get_docstring(node),
            calls=calls
        )
        
    async def _get_function_calls(
        self,
        node,
        imports_map: dict,
        module_path: str,
        class_name: str = None,
    ) -> List[CallInfo]:
        """分析函数调用
        
        Args:
            node: 函数节点
            imports_map: 导入映射
            module_path: 当前模块路径
            class_name: 类名
            
        Returns:
            List[CallInfo]: 调用信息列表
        """
        # Python内置函数列表
        BUILTIN_FUNCTIONS = {
            'len', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple',
            'print', 'range', 'enumerate', 'zip', 'map', 'filter', 'sorted',
            'min', 'max', 'sum', 'any', 'all', 'abs', 'round', 'pow', 'divmod',
            'isinstance', 'issubclass', 'hasattr', 'getattr', 'setattr', 'delattr',
            'id', 'hash', 'type', 'super', 'next', 'iter', 'reversed','strip'
        }
        
        calls = []

        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    # 普通函数调用
                    name = n.func.id
                    if name in imports_map:
                        # 导入的函数调用
                        full_name = imports_map[name]
                        func_name = full_name.split('.')[-1]  # 使用原始函数名
                        calls.append(CallInfo(
                            name=func_name,
                            full_name=full_name,
                            signature=self._build_call_signature(n, func_name)
                        ))
                    else:
                        # 同模块的函数调用
                        if name in BUILTIN_FUNCTIONS:
                            continue

                        full_name = f"{module_path}.{name}"
                        calls.append(CallInfo(
                            name=name,
                            full_name=full_name,
                            signature=self._build_call_signature(n, name)
                        ))                
                elif isinstance(n.func, ast.Attribute):
                    if isinstance(n.func.value, ast.Name):
                        func_name = n.func.attr
                        value_name = n.func.value.id
                        if value_name == "self":
                            # 类方法调用
                            if class_name:
                                full_name = f"{module_path}.{class_name}.{func_name}"
                                calls.append(CallInfo(
                                    name=func_name,
                                    full_name=full_name,
                                    signature=self._build_call_signature(n, func_name)
                                ))
                        else:
                            # 检查是否是导入的模块
                            if value_name in imports_map:
                                base_path = imports_map[value_name]
                                full_name = f"{base_path}.{func_name}"
                                calls.append(CallInfo(
                                    name=func_name,
                                    full_name=full_name,
                                    signature=self._build_call_signature(n, func_name)
                                ))
                            else:
                                full_name = f"{module_path}.{value_name}.{func_name}"
                                calls.append(CallInfo(
                                    name=func_name,
                                    full_name=full_name,
                                    signature=self._build_call_signature(n, func_name)
                                ))
                    elif isinstance(n.func.value, ast.Attribute):
                        # 处理多级调用，如 os.path.join
                        parts = []
                        value = n.func.value
                        while isinstance(value, ast.Attribute):
                            parts.insert(0, value.attr)
                            value = value.value
                        if isinstance(value, ast.Name):
                            base_name = value.id
                            if base_name in imports_map:
                                # 使用导入映射替换基础路径
                                base_path = imports_map[base_name]
                                parts.insert(0, base_path)
                            else:
                                parts.insert(0, base_name)
                            
                            full_path = '.'.join(parts + [n.func.attr])
                            func_name = n.func.attr
                            calls.append(CallInfo(
                                name=func_name,
                                full_name=full_path,
                                signature=self._build_call_signature(n, func_name)
                            ))
        
        return calls

    def _build_call_signature(self, node: ast.Call, func_name: str = None) -> str:
        """构建函数调用签名
        
        Args:
            node: 函数调用节点
            func_name: 已解析的函数名，如果为None则从node中解析
            
        Returns:
            str: 函数签名，格式: "func_name(param_types)->return_type"
            
        Examples:
            process_data(List[str], int) -> Dict
            save(self, data: Dict) -> bool
        """
        # 如果没有传入函数名，则从节点解析
        if func_name is None:
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            else:  # ast.Name
                func_name = node.func.id
        
        # 收集所有参数类型
        arg_types = []
        
        # 处理位置参数
        for arg in node.args:
            if isinstance(arg, ast.Constant):
                arg_types.append(type(arg.value).__name__)
            elif isinstance(arg, ast.Name):
                # 尝试从变量定义或类型注解获取类型
                arg_types.append(self._get_var_type(arg) or 'Any')
            elif isinstance(arg, ast.Call):
                # 函数调用的返回类型
                arg_types.append(self._get_call_return_type(arg) or 'Any')
            else:
                arg_types.append('Any')
        
        # 处理关键字参数
        for keyword in node.keywords:
            if isinstance(keyword.value, ast.Constant):
                arg_types.append(f"{keyword.arg}:{type(keyword.value.value).__name__}")
            elif isinstance(keyword.value, ast.Name):
                arg_type = self._get_var_type(keyword.value) or 'Any'
                arg_types.append(f"{keyword.arg}:{arg_type}")
            else:
                arg_types.append(f"{keyword.arg}:Any")
        
        # 获取返回类型（如果可能）
        return_type = self._get_call_return_type(node) or 'Any'
        
        return f"{func_name}({','.join(arg_types)})->{return_type}"

    def _get_var_type(self, node: ast.Name) -> Optional[str]:
        """尝试获取变量的类型"""
        # 这里可以实现更复杂的类型推断
        # 当前简单返回 None，后续可以扩展
        return None

    def _get_call_return_type(self, node: ast.Call) -> Optional[str]:
        """尝试获取函数调用的返回类型"""
        # 这里可以实现更复杂的返回类型推断
        # 当前简单返回 None，后续可以扩展
        return None

    async def analyze_class(
        self,
        node: ast.ClassDef,
        imports_map: dict,
    ) -> Tuple[ClassInfo, Set[ast.FunctionDef]]:
        """分析Python类"""
        source_code = ast.unparse(node)
        
        # 获取当前模块路径
        module_path = self._get_module_path()
        
        # 生成完整类名
        full_name = f"{module_path}.{node.name}"

        # 确定类的类型
        node_type = ClassType.CLASS  # 默认为普通类
        # 检查是否是接口/抽象类
        if any(base.id == 'ABC' for base in node.bases if isinstance(base, ast.Name)):
            node_type = ClassType.INTERFACE
        
        # 分析父类
        base_classes = self._get_base_classes(node, imports_map, module_path)
        
        methods = []
        attributes = []
        # 记录类方法的集合，用于后续过滤顶层函数
        class_methods = set()
        
        # 分析类成员
        for item in node.body:
            # 处理所有类型的方法定义
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 记录类方法，用于后续过滤顶层函数
                class_methods.add(item)

                # 转换为方法类型
                method = await self.analyze_function(
                    node=item,
                    imports_map=imports_map,
                    class_name=node.name,
                )
                method.type = FunctionType.METHOD.value
                method.class_name = node.name
                method.full_name = f"{module_path}.{node.name}.{method.name}"
                methods.append(method)
            elif isinstance(item, ast.AnnAssign):
                if isinstance(item.target, ast.Name):
                    attributes.append(item.target.id)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attributes.append(target.id)
        
        class_info = ClassInfo(
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            name=node.name,
            full_name=full_name,
            node_type=node_type.value,
            source_code=source_code,
            start_line=node.lineno,
            end_line=node.end_lineno,
            methods=methods,
            attributes=attributes,
            base_classes=base_classes,
            docstring=ast.get_docstring(node)
        )

        return class_info, class_methods

    def _get_base_classes(self, node: ast.ClassDef, imports_map: dict, module_path: str) -> List[ClassInfo]:
        """获取类的父类列表
        
        处理两种基类引用情况：
        1. 通过 import 导入并可能重命名的基类
           例如：from app.base import BaseClass as Base
                class MyClass(Base): ...
        
        2. 直接使用多级引用的基类
           例如：class MyClass(app.base.BaseClass): ...
        """
        base_classes = []
        
        for base in node.bases:
            if isinstance(base, ast.Name):
                # 跳过内置类型和特殊类型
                if base.id in ("object", "ABC", "Protocol"):
                    continue
                    
                # 情况1：处理可能通过 import 重命名的基类
                # 从 imports_map 中查找完整路径，如果没有则假设在当前模块
                base_full_name = imports_map.get(base.id, f"{module_path}.{base.id}")
                # 从完整路径中提取实际的类名
                base_name = base_full_name.split('.')[-1]
                
                base_classes.append(ClassInfo(
                    file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
                    name=base_name,
                    full_name=base_full_name,
                    node_type=ClassType.CLASS.value
                ))
            
            elif isinstance(base, ast.Attribute):
                # 情况2：处理多级引用的基类
                # 使用 ast.unparse 获取完整的引用路径
                base_full_name = ast.unparse(base)
                
                # 如果是通过 import as 重命名的模块中的类
                # 例如：import app.base as base_module
                #      class MyClass(base_module.BaseClass): ...
                if isinstance(base.value, ast.Name) and base.value.id in imports_map:
                    module_prefix = imports_map[base.value.id]
                    base_full_name = f"{module_prefix}.{base.attr}"
                
                # 从完整路径中提取实际的类名
                base_name = base_full_name.split('.')[-1]
                
                base_classes.append(ClassInfo(
                    file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
                    name=base_name,
                    full_name=base_full_name,
                    node_type=ClassType.CLASS.value
                ))
        
        return base_classes

    def _get_function_params(self, node) -> List[str]:
        """获取函数参数列表
        
        Args:
            node: 函数定义节点
            
        Returns:
            参数名列表，包括位置参数、默认参数、*args和**kwargs
        """
        params = []
        
        # 处理位置参数和默认参数
        for arg in node.args.args:
            params.append(arg.arg)
        
        # 处理 *args
        if node.args.vararg:
            params.append(f"*{node.args.vararg.arg}")
        
        # 处理关键字参数
        for arg in node.args.kwonlyargs:
            params.append(arg.arg)
        
        # 处理 **kwargs
        if node.args.kwarg:
            params.append(f"**{node.args.kwarg.arg}")
        
        return params

    def _get_function_returns(self, node) -> List[str]:
        """获取函数返回值列表
        
        Args:
            node: 函数定义节点
            
        Returns:
            返回值列表。通过分析:
            1. return 语句
            2. yield 语句
            3. 返回值类型注解
        """
        returns = []
        
        # 分析 return 语句
        for n in ast.walk(node):
            if isinstance(n, ast.Return) and n.value:
                if isinstance(n.value, ast.Name):
                    returns.append(n.value.id)
                elif isinstance(n.value, ast.Constant):
                    returns.append(type(n.value.value).__name__)
                elif isinstance(n.value, ast.Call):
                    if isinstance(n.value.func, ast.Name):
                        returns.append(n.value.func.id)
                    elif isinstance(n.value.func, ast.Attribute):
                        returns.append(n.value.func.attr)
                    
        # 分析 yield 语句
        for n in ast.walk(node):
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                returns.append("Generator")
                break
            
        # 分析返回值类型注解
        if node.returns:
            if isinstance(node.returns, ast.Name):
                returns.append(node.returns.id)
            elif isinstance(node.returns, ast.Constant):
                returns.append(str(node.returns.value))
            
        # 如果没有找到任何返回值信息
        if not returns:
            returns.append("None")
        
        return list(set(returns))  # 去重 

    def _get_module_path(self) -> str:
        """由当前分析文件的相对路径推出「模块点路径」，用于相对 import 解析与 full_name 前缀。

        规则：相对 self.base_path 的路径去掉 .py 后缀，路径分隔符换成点号。
        非 .py 后缀时仍做 / -> .，用于特殊扩展名文件占位。
        """
        rel = normalize_path(os.path.relpath(self.file_path, self.base_path)).strip()
        if not rel.lower().endswith(".py"):
            return rel.replace("/", ".")
        base = rel[:-3]
        if not base:
            return ""
        return base.replace("/", ".")

    def _dotted_to_repo_rel(self, dotted: str) -> Optional[str]:
        """将模块点路径转换为仓库内相对路径
        input: dotted: 模块点路径
        output: 仓库内相对路径
        Examples:
            app.utils.helper -> app/utils/helper.py
            app.config.settings -> app/config/settings.py
            app.models.user -> app/models/user.py
            app.models.user.User -> app/models/user/User.py
            app.models.user.User.User -> app/models/user/User/User.py
            app.models.user.User.User.User -> app/models/user/User/User/User.py
        """
        if not dotted:
            return None
        
        rel_base = dotted.replace(".", os.sep)
        for rel in (f"{rel_base}.py", os.path.join(rel_base, "__init__.py")):
            # 将操作系统路径转换为绝对路径
            abs_path = os.path.join(self.base_path, rel)
            # 判断是否是文件
            if os.path.isfile(abs_path):
                return normalize_path(os.path.relpath(abs_path, self.base_path))
        return None

    def _dependent_files_from_import_module_prefixes(self, import_module_prefixes: Set[str]) -> List[str]:
        """把 import 归纳出的模块点路径集合，转换成本仓库内被依赖文件的相对路径列表。

        功能分步：
        1) 点路径到磁盘：对每个点路径依次尝试「同名单文件 .py」「目录包下的 __init__.py」，在 self.base_path 下存在则记为依赖。
        2) 展开包入口：若依赖落在某包 __init__.py，则静态解析该文件中的 import（以该包目录对应的点路径为 current_module），
           将再导出所指向的子模块、子包继续映射为路径并入集合，避免「只依赖到空壳 __init__、实现其实在子文件」时边丢失。
        3) 对已出现的 __init__.py 做队列 BFS；seen_init 避免循环 re-export 死循环。始终排除当前正在分析的本文件路径。
        4) 返回去重、排序后的路径列表。

        局限：仅 AST 静态可见的 import；动态 import、仅 __all__ 字符串列表等无法覆盖。

        说明：内部嵌套 dotted_to_repo_rel 仅复用「点路径 -> 仓库相对路径」判定，不单独成类方法。

        input: import_module_prefixes: 模块点路径集合
        output: 仓库内相对路径列表
        Examples:
            [app.utils.helper, app.config.settings, app.models.user] -> [app/utils/helper.py, app/config/settings.py, app/models/user.py]
        """
        cur_file_rel_path = normalize_path(os.path.relpath(self.file_path, self.base_path))


        # 1) 点路径到磁盘：对每个点路径依次尝试「同名单文件 .py」「目录包下的 __init__.py」，在 self.base_path 下存在则记为依赖。
        dependent_files: Set[str] = set()
        for dotted in import_module_prefixes:
            if not dotted:
                continue
            found = self._dotted_to_repo_rel(dotted)
            if found and found != cur_file_rel_path:
                dependent_files.add(found)

        # 2) 若依赖落在某包 __init__.py，则静态解析该文件中的 import（以该包目录对应的点路径为 current_module），
        seen_init: Set[str] = set()
        queue = deque(f for f in dependent_files if f.endswith("__init__.py"))
        while queue:
            init_file_rel_path = queue.popleft()
            if init_file_rel_path in seen_init:
                continue
            seen_init.add(init_file_rel_path)

            abs_init_file_path = os.path.join(self.base_path, init_file_rel_path.replace("/", os.sep))
            if not os.path.isfile(abs_init_file_path):
                continue
            try:
                with open(abs_init_file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            
            # 解析 __init__.py 文件
            try:                
                tree = ast.parse(content)
            except SyntaxError:
                continue

            np = normalize_path(init_file_rel_path).strip()
            base_dir = np[: -len("__init__.py")].rstrip("/")
            if not base_dir:
                continue
            pkg = base_dir.replace("/", ".")
            # 以 __init__.py 自身作为“当前模块”来解析相对导入，避免相对层级偏移。
            sub_imports_map, sub_import_module_prefixes = self._analyze_imports(
                tree, f"{pkg}.__init__"
            )

            # 仅展开业务实际请求的 pkg.<symbol>，减少把 __init__.py 里所有 re-export 全量并入 dependent_files 的情况。
            requested_symbols: Set[str] = set()
            pkg_prefix = f"{pkg}."
            for dotted in import_module_prefixes:
                if dotted.startswith(pkg_prefix):
                    suffix = dotted[len(pkg_prefix):]
                    # 在这里把 “pkg.<symbol>” 视作请求符号；若是 “pkg.sub.attr” 这类更深层路径，交给对应子包 __init__.py 展开。
                    if suffix and "." not in suffix:
                        requested_symbols.add(suffix)

            if requested_symbols:
                missing_symbol = False
                for sym in requested_symbols:
                    target = sub_imports_map.get(sym)
                    if not target:
                        missing_symbol = True
                        continue

                    # target 可能是 “pkg.submod.<Attr>” 或 “pkg.submod”（当 sym 本身是子模块时）。
                    found = self._dotted_to_repo_rel(target)
                    if not found:
                        parts = target.split(".")
                        if len(parts) > 1:
                            found = self._dotted_to_repo_rel(".".join(parts[:-1]))

                    if not found or found == cur_file_rel_path:
                        continue
                    if found not in dependent_files:
                        dependent_files.add(found)
                        if found.endswith("__init__.py"):
                            queue.append(found)
                # 若找不到部分 re-export 的导入来源，就只能退回保守展开，避免 dependent_files 少量缺失。
                if missing_symbol:
                    for sub_dotted in sub_import_module_prefixes:
                        found = self._dotted_to_repo_rel(sub_dotted)
                        if not found or found == cur_file_rel_path:
                            continue
                        if found not in dependent_files:
                            dependent_files.add(found)
                            if found.endswith("__init__.py"):
                                queue.append(found)
            else:
                # 若业务没具体请求到 pkg.<symbol>（例如只 import pkg），则只能保守展开，避免漏掉潜在依赖。
                for sub_dotted in sub_import_module_prefixes:
                    found = self._dotted_to_repo_rel(sub_dotted)
                    if not found or found == cur_file_rel_path:
                        continue
                    if found not in dependent_files:
                        dependent_files.add(found)
                        if found.endswith("__init__.py"):
                            queue.append(found)
        return sorted(dependent_files)