from __future__ import annotations
import ast
import os
import re
from typing import List, Optional, Set
from ...schemes.public_api import PublicApi
from ....repo_analysis.services.codeast.model import ClassInfo, FileInfo, FunctionInfo, Language
from app.utils.common import normalize_path


class PublicApiExtractor:
    """从 AST FileInfo 中按语言惯例过滤公开接口。"""

    _STATIC_DECL = re.compile(r"\bstatic\b")
    _EXPORT_DECL = re.compile(r"\bexport\b")
    _PUB_DECL = re.compile(r"\bpub\b")
    _JAVA_PUBLIC = re.compile(r"\bpublic\b")
    _MODULE_EXPORTS = re.compile(r"\bmodule\.exports\b|\bexports\.")

    @classmethod
    def should_skip_file(cls, rel_file_path: str) -> bool:
        path = normalize_path(rel_file_path or "").strip("/")
        lower = path.lower()
        padded = f"/{lower}/"
        for marker in ("/tests/", "/test/", "/__tests__/", "/src/test/"):
            if marker in padded:
                return True
        base = os.path.basename(lower)
        if base.startswith("test_") and base.endswith((".py", ".rs", ".c", ".cpp", ".cc", ".cxx")):
            return True
        if base.endswith("_test.py") or base.endswith("_test.go") or base.endswith("_test.rs"):
            return True
        if base.endswith("test.java") or base.endswith("tests.java"):
            return True
        if ".test." in base or ".spec." in base:
            return True
        return False

    @classmethod
    def extract(
        cls,
        file_info: Optional[FileInfo],
        *,
        source: Optional[str] = None,
    ) -> List[PublicApi]:
        if not file_info:
            return []
        if cls.should_skip_file(file_info.file_path or ""):
            return []

        language = cls._normalize_language(file_info.language)
        if language == Language.PYTHON.value:
            return cls._extract_python(file_info, source or "")
        if language == Language.GO.value:
            return cls._extract_go(file_info)
        if language == Language.JAVA.value:
            return cls._extract_java(file_info)
        if language == Language.C.value:
            return cls._extract_c_family(file_info, Language.C.value)
        if language == Language.CPP.value:
            return cls._extract_c_family(file_info, Language.CPP.value)
        if language == Language.JAVASCRIPT.value:
            return cls._extract_js_family(file_info, source or "", Language.JAVASCRIPT.value)
        if language == Language.TYPESCRIPT.value:
            return cls._extract_js_family(file_info, source or "", Language.TYPESCRIPT.value)
        if language == Language.RUST.value:
            return cls._extract_rust(file_info)
        return []

    @staticmethod
    def _normalize_language(language: object) -> str:
        if isinstance(language, Language):
            return language.value
        return str(language or "").strip().lower()

    @classmethod
    def _extract_python(cls, file_info: FileInfo, source: str) -> List[PublicApi]:
        allowed = cls._python_all_names(source)
        apis: List[PublicApi] = []
        for fn in file_info.functions or []:
            if not cls._python_is_public_name(fn.name, allowed):
                continue
            apis.append(cls._from_function(fn, file_info, "function", Language.PYTHON.value))
        for clz in file_info.classes or []:
            if not cls._python_is_public_name(clz.name, allowed):
                continue
            apis.append(cls._from_class(clz, file_info, Language.PYTHON.value))
            for method in clz.methods or []:
                if not cls._python_is_public_name(method.name, allowed=None):
                    continue
                if method.name == "__init__":
                    continue
                apis.append(
                    cls._from_function(
                        method,
                        file_info,
                        "method",
                        Language.PYTHON.value,
                        class_name=clz.name,
                    )
                )
        return apis

    @staticmethod
    def _python_all_names(source: str) -> Optional[Set[str]]:
        if not (source or "").strip():
            return None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    names: Set[str] = set()
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                names.add(elt.value)
                    return names
        return None

    @staticmethod
    def _python_is_public_name(name: Optional[str], allowed: Optional[Set[str]]) -> bool:
        n = (name or "").strip()
        if not n:
            return False
        if allowed is not None:
            return n in allowed
        return not n.startswith("_")

    @classmethod
    def _extract_go(cls, file_info: FileInfo) -> List[PublicApi]:
        apis: List[PublicApi] = []
        for fn in file_info.functions or []:
            if not cls._go_exported(fn.name):
                continue
            apis.append(cls._from_function(fn, file_info, "function", Language.GO.value))
        for clz in file_info.classes or []:
            if not cls._go_exported(clz.name):
                continue
            apis.append(cls._from_class(clz, file_info, Language.GO.value))
            for method in clz.methods or []:
                if not cls._go_exported(method.name):
                    continue
                apis.append(
                    cls._from_function(
                        method,
                        file_info,
                        "method",
                        Language.GO.value,
                        class_name=clz.name,
                    )
                )
        return apis

    @staticmethod
    def _go_exported(name: Optional[str]) -> bool:
        n = (name or "").strip()
        return bool(n) and n[0].isupper()

    @classmethod
    def _extract_java(cls, file_info: FileInfo) -> List[PublicApi]:
        apis: List[PublicApi] = []
        for fn in file_info.functions or []:
            if not cls._java_is_public(fn.source_code, fn.name):
                continue
            apis.append(cls._from_function(fn, file_info, "function", Language.JAVA.value))
        for clz in file_info.classes or []:
            if not cls._java_is_public(clz.source_code, clz.name):
                continue
            apis.append(cls._from_class(clz, file_info, Language.JAVA.value))
            for method in clz.methods or []:
                if not cls._java_is_public(method.source_code, method.name):
                    continue
                apis.append(
                    cls._from_function(
                        method,
                        file_info,
                        "method",
                        Language.JAVA.value,
                        class_name=clz.name,
                    )
                )
        return apis

    @classmethod
    def _java_is_public(cls, source_code: Optional[str], name: Optional[str]) -> bool:
        src = (source_code or "").strip()
        if not src:
            return False
        head = "\n".join(src.splitlines()[:8])
        if not cls._JAVA_PUBLIC.search(head):
            return False
        n = (name or "").strip()
        return bool(n) and not n.startswith("_")

    @classmethod
    def _extract_c_family(cls, file_info: FileInfo, language: str) -> List[PublicApi]:
        apis: List[PublicApi] = []
        for fn in file_info.functions or []:
            if not cls._c_family_is_public(fn.source_code, fn.name):
                continue
            apis.append(cls._from_function(fn, file_info, "function", language))
        for clz in file_info.classes or []:
            if not cls._c_family_is_public(clz.source_code, clz.name):
                continue
            apis.append(cls._from_class(clz, file_info, language))
            for method in clz.methods or []:
                if not cls._c_family_is_public(method.source_code, method.name):
                    continue
                apis.append(
                    cls._from_function(
                        method,
                        file_info,
                        "method",
                        language,
                        class_name=clz.name,
                    )
                )
        return apis

    @classmethod
    def _c_family_is_public(cls, source_code: Optional[str], name: Optional[str]) -> bool:
        n = (name or "").strip()
        if not n or n.startswith("_"):
            return False
        src = (source_code or "").strip()
        if not src:
            return True
        head = "\n".join(src.splitlines()[:6])
        return not cls._STATIC_DECL.search(head)

    @classmethod
    def _extract_js_family(cls, file_info: FileInfo, source: str, language: str) -> List[PublicApi]:
        file_source = source or ""
        apis: List[PublicApi] = []
        for fn in file_info.functions or []:
            if not cls._js_is_public(fn.source_code, fn.name, file_source):
                continue
            apis.append(cls._from_function(fn, file_info, "function", language))
        for clz in file_info.classes or []:
            if not cls._js_is_public(clz.source_code, clz.name, file_source):
                continue
            apis.append(cls._from_class(clz, file_info, language))
            for method in clz.methods or []:
                n = (method.name or "").strip()
                if not n or n.startswith("_"):
                    continue
                apis.append(
                    cls._from_function(
                        method,
                        file_info,
                        "method",
                        language,
                        class_name=clz.name,
                    )
                )
        return apis

    @classmethod
    def _js_is_public(
        cls,
        source_code: Optional[str],
        name: Optional[str],
        file_source: str,
    ) -> bool:
        n = (name or "").strip()
        if not n or n.startswith("_"):
            return False
        src = (source_code or "").strip()
        if src and cls._EXPORT_DECL.search("\n".join(src.splitlines()[:6])):
            return True
        if re.search(rf"\bexport\b[^\n;{{]*\b{re.escape(n)}\b", file_source):
            return True
        if re.search(rf"\b(?:module\.exports|exports)\s*\.\s*{re.escape(n)}\b", file_source):
            return True
        if not cls._EXPORT_DECL.search(file_source) and not cls._MODULE_EXPORTS.search(file_source):
            return True
        return False

    @classmethod
    def _extract_rust(cls, file_info: FileInfo) -> List[PublicApi]:
        apis: List[PublicApi] = []
        for fn in file_info.functions or []:
            if not cls._rust_is_public(fn.source_code, fn.name):
                continue
            apis.append(cls._from_function(fn, file_info, "function", Language.RUST.value))
        for clz in file_info.classes or []:
            if not cls._rust_is_public(clz.source_code, clz.name):
                continue
            apis.append(cls._from_class(clz, file_info, Language.RUST.value))
            for method in clz.methods or []:
                if not cls._rust_is_public(method.source_code, method.name):
                    continue
                apis.append(
                    cls._from_function(
                        method,
                        file_info,
                        "method",
                        Language.RUST.value,
                        class_name=clz.name,
                    )
                )
        return apis

    @classmethod
    def _rust_is_public(cls, source_code: Optional[str], name: Optional[str]) -> bool:
        n = (name or "").strip()
        if not n or n.startswith("_"):
            return False
        src = (source_code or "").strip()
        if not src:
            return False
        head = "\n".join(src.splitlines()[:8])
        return bool(cls._PUB_DECL.search(head))

    @classmethod
    def _from_function(
        cls,
        fn: FunctionInfo,
        file_info: FileInfo,
        kind: str,
        language: str,
        class_name: Optional[str] = None,
    ) -> PublicApi:
        return PublicApi(
            name=fn.name or "",
            kind=kind,
            signature=(fn.signature or fn.name or "").strip(),
            file_path=normalize_path(fn.file_path or file_info.file_path or ""),
            language=language,
            start_line=int(fn.start_line or 1),
            end_line=int(fn.end_line or max(fn.start_line or 1, 1)),
            params=list(fn.params or []),
            param_types=list(fn.param_types or []),
            returns=list(fn.returns or []),
            return_types=list(fn.return_types or []),
            docstring=fn.docstring,
            source_code=(fn.source_code or "").strip(),
            class_name=class_name or fn.class_name,
        )

    @classmethod
    def _from_class(cls, clz: ClassInfo, file_info: FileInfo, language: str) -> PublicApi:
        sig = clz.name or ""
        if clz.node_type:
            sig = f"{clz.node_type} {clz.name}"
        return PublicApi(
            name=clz.name or "",
            kind="class",
            signature=sig.strip(),
            file_path=normalize_path(clz.file_path or file_info.file_path or ""),
            language=language,
            start_line=int(clz.start_line or 1),
            end_line=int(clz.end_line or max(clz.start_line or 1, 1)),
            docstring=clz.docstring,
            source_code=(clz.source_code or "").strip(),
        )
