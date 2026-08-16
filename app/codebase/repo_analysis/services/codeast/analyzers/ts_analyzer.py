import logging
import os
import re
from typing import List, Optional, Set
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser
from app.utils.common import normalize_path
from .base import LanguageAnalyzer
from ..model import FileInfo, FunctionInfo, ClassInfo, ClassType, FunctionType, Language as Lang


LANGUAGES = {}


def get_parser(kind: str):
    if kind not in LANGUAGES:
        try:
            lang_fn = tsts.language_tsx if kind == "tsx" else tsts.language_typescript
            parser = Parser()
            parser.language = Language(lang_fn())
            LANGUAGES[kind] = parser
        except Exception as e:
            logging.error("Error loading TypeScript language kind=%s: %s", kind, e)
            return None
    return LANGUAGES.get(kind)


class TsAnalyzer(LanguageAnalyzer):
    """TypeScript / TSX AST 分析（tree-sitter-typescript）。"""

    def __init__(self, base_path: str, file_path: str):
        super().__init__(base_path, file_path)
        ext = os.path.splitext(file_path)[1].lower()
        self.parser = get_parser("tsx" if ext == ".tsx" else "typescript")

    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        if self.parser is None:
            logging.error("TypeScript parser is not initialized")
            return None
        try:
            content = source if source is not None else self._read_source_file()
            tree = self.parser.parse(bytes(content, "utf8"))
            if not tree:
                return None
            functions: List[FunctionInfo] = []
            classes: List[ClassInfo] = []

            async def visit_node(node):
                if node.type in (
                    "function_declaration",
                    "generator_function_declaration",
                    "function_signature",
                ):
                    fn = await self._try_create_function_node(
                        node, content, is_method=False, class_name=None
                    )
                    if fn:
                        functions.append(fn)
                elif node.type in ("class_declaration", "abstract_class_declaration"):
                    cls_node = await self._create_class_node(node, content)
                    if cls_node:
                        classes.append(cls_node)
                elif node.type == "export_statement":
                    for child in node.children:
                        await visit_node(child)
                    return
                for child in node.children:
                    await visit_node(child)

            await visit_node(tree.root_node)
            imports = self.get_imports(content)
            cur_rel = normalize_path(os.path.relpath(self.file_path, self.base_path))
            dep_paths = self._dependent_files_from_imports(imports, cur_rel)
            return FileInfo(
                name=os.path.basename(self.file_path),
                file_path=cur_rel,
                language=Lang.TYPESCRIPT,
                functions=functions,
                classes=classes,
                imports=imports,
                dependent_files=dep_paths,
            )
        except Exception as e:
            logging.error("Error analyzing TypeScript file %s: %s", self.file_path, e)
            return None

    def get_imports(self, content: str) -> List[str]:
        out: List[str] = []
        for pattern in (
            r"""from\s+['\"]([^'\"]+)['\"]""",
            r"""import\s+['\"]([^'\"]+)['\"]""",
            r"""require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)""",
            r"""import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)""",
        ):
            for m in re.finditer(pattern, content):
                spec = m.group(1).strip()
                if spec and not spec.startswith("node:"):
                    out.append(spec)
        return list(dict.fromkeys(out))

    def _dependent_files_from_imports(self, imports: List[str], cur_file_rel_path: str) -> List[str]:
        dependent: Set[str] = set()
        cur_dir = os.path.dirname(self.file_path)

        def add_if_repo_file(abs_path: str) -> None:
            if not os.path.isfile(abs_path):
                return
            rel = normalize_path(os.path.relpath(abs_path, self.base_path))
            if rel.startswith("../") or rel == cur_file_rel_path:
                return
            dependent.add(rel)

        for spec in imports:
            abs_resolved = self._resolve_ts_import_to_abs(spec, cur_dir)
            if abs_resolved:
                add_if_repo_file(abs_resolved)
        return sorted(dependent)

    def _resolve_ts_import_to_abs(self, spec: str, cur_dir: str) -> Optional[str]:
        spec = spec.strip().replace("\\", "/")
        if not spec or spec.startswith("node:"):
            return None
        candidates: List[str] = []
        if spec.startswith("./") or spec.startswith("../"):
            candidates.append(os.path.normpath(os.path.join(cur_dir, spec.replace("/", os.sep))))
        else:
            candidates.append(os.path.normpath(os.path.join(self.base_path, spec.replace("/", os.sep))))
            candidates.append(os.path.normpath(os.path.join(cur_dir, spec.replace("/", os.sep))))
        suffixes = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")
        for root in candidates:
            if os.path.isfile(root):
                return root
            root_lower = root.lower()
            if any(root_lower.endswith(suf) for suf in suffixes):
                continue
            for suf in suffixes:
                p = root + suf
                if os.path.isfile(p):
                    return p
            for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
                p = os.path.join(root, idx)
                if os.path.isfile(p):
                    return p
        return None

    def _callable_name(self, node) -> str:
        for child in node.children:
            if child.type in ("identifier", "property_identifier", "type_identifier"):
                return child.text.decode("utf8")
        return ""

    def _class_name(self, node) -> str:
        for ch in node.children:
            if ch.type in ("type_identifier", "identifier"):
                return ch.text.decode("utf8")
        return ""

    def _formal_parameters_text(self, node, content: str) -> str:
        for child in node.children:
            if child.type in ("formal_parameters", "required_parameters"):
                return content[child.start_byte : child.end_byte].strip()
        return ""

    async def _try_create_function_node(
        self, node, content: str, *, is_method: bool, class_name: Optional[str]
    ) -> Optional[FunctionInfo]:
        name = self._callable_name(node)
        if not name or name.startswith("_"):
            return None
        source_code = content[node.start_byte : node.end_byte]
        params_txt = self._formal_parameters_text(node, content)
        signature = f"{name}{params_txt} -> void"
        fn_type = FunctionType.METHOD.value if is_method else FunctionType.FUNCTION.value
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
            return_types=["void"],
            docstring="",
            class_name=class_name,
        )

    async def _create_class_node(self, node, content: str) -> Optional[ClassInfo]:
        cls_name = self._class_name(node)
        if not cls_name or cls_name.startswith("_"):
            return None
        source_code = content[node.start_byte : node.end_byte]
        methods: List[FunctionInfo] = []
        for child in node.children:
            if child.type != "class_body":
                continue
            for m in child.children:
                if m.type in ("method_definition", "public_field_definition", "method_signature"):
                    if m.type == "public_field_definition":
                        continue
                    mn = await self._try_create_function_node(
                        m, content, is_method=True, class_name=cls_name
                    )
                    if mn:
                        methods.append(mn)
        return ClassInfo(
            name=cls_name,
            full_name=cls_name,
            file_path=normalize_path(os.path.relpath(self.file_path, self.base_path)),
            node_type=ClassType.CLASS.value,
            source_code=source_code,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            methods=methods,
            attributes=[],
            docstring="",
        )
