import logging
import os
import re
from typing import List, Optional, Set
import tree_sitter_rust as tsrust
from tree_sitter import Language, Parser
from app.utils.common import normalize_path
from .base import LanguageAnalyzer
from ..model import FileInfo, FunctionInfo, ClassInfo, ClassType, FunctionType, Language as Lang


LANGUAGES = {}


def get_language():
    if "rust" not in LANGUAGES:
        try:
            parser = Parser()
            parser.language = Language(tsrust.language())
            LANGUAGES["rust"] = parser
        except Exception as e:
            logging.error("Error loading Rust language: %s", e)
            return None
    return LANGUAGES.get("rust")


class RustAnalyzer(LanguageAnalyzer):
    """Rust AST 分析（tree-sitter-rust）。"""

    def __init__(self, base_path: str, file_path: str):
        super().__init__(base_path, file_path)
        self.parser = get_language()

    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        if self.parser is None:
            logging.error("Rust parser is not initialized")
            return None
        try:
            content = source if source is not None else self._read_source_file()
            tree = self.parser.parse(bytes(content, "utf8"))
            if not tree:
                return None
            functions: List[FunctionInfo] = []
            classes: List[ClassInfo] = []

            async def visit_node(node):
                if node.type == "function_item":
                    fn = await self._create_function_node(node, content, class_name=None)
                    if fn:
                        functions.append(fn)
                elif node.type in ("struct_item", "enum_item", "trait_item", "union_item"):
                    cls_node = await self._create_type_node(node, content)
                    if cls_node:
                        classes.append(cls_node)
                elif node.type == "impl_item":
                    impl_cls = await self._create_impl_node(node, content)
                    if impl_cls:
                        classes.append(impl_cls)
                for child in node.children:
                    await visit_node(child)

            await visit_node(tree.root_node)
            imports = self.get_imports(content)
            cur_rel = normalize_path(os.path.relpath(self.file_path, self.base_path))
            dep_paths = self._dependent_files_from_imports(imports, cur_rel)
            return FileInfo(
                name=os.path.basename(self.file_path),
                file_path=cur_rel,
                language=Lang.RUST,
                functions=functions,
                classes=classes,
                imports=imports,
                dependent_files=dep_paths,
            )
        except Exception as e:
            logging.error("Error analyzing Rust file %s: %s", self.file_path, e)
            return None

    def get_imports(self, content: str) -> List[str]:
        out: List[str] = []
        for m in re.finditer(r"(?m)^\s*use\s+([^;{]+)", content):
            spec = m.group(1).strip()
            if spec:
                out.append(spec.split("::")[0].strip())
        return list(dict.fromkeys(out))

    def _dependent_files_from_imports(self, imports: List[str], cur_file_rel_path: str) -> List[str]:
        dependent: Set[str] = set()
        for mod in imports:
            if not mod or mod in {"crate", "super", "self", "std", "core", "alloc"}:
                continue
            for cand in (
                os.path.join(self.base_path, "src", f"{mod}.rs"),
                os.path.join(self.base_path, "src", mod, "mod.rs"),
                os.path.join(os.path.dirname(self.file_path), f"{mod}.rs"),
                os.path.join(os.path.dirname(self.file_path), mod, "mod.rs"),
            ):
                if not os.path.isfile(cand):
                    continue
                rel = normalize_path(os.path.relpath(cand, self.base_path))
                if rel.startswith("../") or rel == cur_file_rel_path:
                    continue
                dependent.add(rel)
        return sorted(dependent)

    def _child_text(self, node, types: Set[str]) -> str:
        for child in node.children:
            if child.type in types:
                return child.text.decode("utf8")
        return ""

    async def _create_function_node(
        self, node, content: str, *, class_name: Optional[str]
    ) -> Optional[FunctionInfo]:
        name = self._child_text(node, {"identifier"})
        if not name or name.startswith("_"):
            return None
        source_code = content[node.start_byte : node.end_byte]
        params = ""
        for child in node.children:
            if child.type == "parameters":
                params = content[child.start_byte : child.end_byte].strip()
                break
        signature = f"fn {name}{params}"
        fn_type = FunctionType.METHOD.value if class_name else FunctionType.FUNCTION.value
        full_name = f"{class_name}.{name}" if class_name else name
        return FunctionInfo(
            name=name,
            full_name=full_name,
            signature=signature,
            type=fn_type,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            source_code=source_code,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            params=[],
            param_types=[],
            returns=[],
            return_types=[],
            docstring="",
            class_name=class_name,
        )

    async def _create_type_node(self, node, content: str) -> Optional[ClassInfo]:
        name = self._child_text(node, {"type_identifier", "identifier"})
        if not name or name.startswith("_"):
            return None
        node_type = ClassType.STRUCT.value
        if node.type == "enum_item":
            node_type = ClassType.CLASS.value
        elif node.type == "trait_item":
            node_type = ClassType.INTERFACE.value
        source_code = content[node.start_byte : node.end_byte]
        return ClassInfo(
            name=name,
            full_name=name,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            node_type=node_type,
            source_code=source_code,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            methods=[],
            attributes=[],
            docstring="",
        )

    async def _create_impl_node(self, node, content: str) -> Optional[ClassInfo]:
        type_name = ""
        for child in node.children:
            if child.type == "type_identifier":
                type_name = child.text.decode("utf8")
        if not type_name:
            return None
        methods: List[FunctionInfo] = []
        for child in node.children:
            if child.type != "declaration_list":
                continue
            for item in child.children:
                if item.type == "function_item":
                    fn = await self._create_function_node(item, content, class_name=type_name)
                    if fn:
                        methods.append(fn)
        if not methods:
            return None
        source_code = content[node.start_byte : node.end_byte]
        return ClassInfo(
            name=type_name,
            full_name=type_name,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            node_type=ClassType.CLASS.value,
            source_code=source_code,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            methods=methods,
            attributes=[],
            docstring="",
        )
