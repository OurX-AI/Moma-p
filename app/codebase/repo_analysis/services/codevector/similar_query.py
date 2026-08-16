from __future__ import annotations
import re
from typing import List, Optional, Set
from ..nl2code_enhance.gate import NlToCodeEnhancement
from ..nl2code_enhance.keyword_expander import LexiconExpandable
from ..nl2code_enhance.query_builder import NlCodeQueryBuilder


_PY_DEF = re.compile(
    r"(?m)^\s*(async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)",
)
_JAVA_GO_DEF = re.compile(
    r"(?m)^\s*(public|private|protected)?\s*(static\s+)?(class|interface|enum|func|void|[\w<>\[\]]+)\s+([A-Za-z_][A-Za-z0-9_]*)",
)
_COMMENT_BLOCK = re.compile(r'(?s)"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')


class SimilarQueryNormalizer:
    """similar 检索前的 query 归一化与多视角 embedding 文本构造。"""

    @staticmethod
    def normalize(code_text: str) -> str:
        text = (code_text or "").strip()
        if not text:
            return ""
        text = _COMMENT_BLOCK.sub("", text)
        cleaned_lines: List[str] = []
        for ln in text.splitlines():
            stripped = ln.rstrip()
            if not stripped.strip():
                continue
            if stripped.lstrip().startswith("#"):
                continue
            if "//" in stripped:
                stripped = stripped.split("//", 1)[0].rstrip()
            if "#" in stripped:
                in_str = False
                quote = ""
                cut = len(stripped)
                for i, ch in enumerate(stripped):
                    if ch in ("'", '"'):
                        if not in_str:
                            in_str = True
                            quote = ch
                            continue
                        if ch == quote:
                            in_str = False
                        continue
                    if not in_str and ch == "#":
                        cut = i
                        break
                stripped = stripped[:cut].rstrip()
            if stripped:
                cleaned_lines.append(stripped)
        return "\n".join(cleaned_lines)

    @staticmethod
    def extract_symbol_names(code_text: str) -> Set[str]:
        names: Set[str] = set()
        for match in _PY_DEF.finditer(code_text or ""):
            name = match.group(2)
            if name and len(name) >= 2:
                names.add(name)
        for match in _JAVA_GO_DEF.finditer(code_text or ""):
            name = match.group(4) if match.lastindex and match.lastindex >= 4 else None
            if name and len(name) >= 2:
                names.add(name)
        return names

    @staticmethod
    def signature_lines(code_text: str) -> str:
        lines: List[str] = []
        for ln in (code_text or "").splitlines():
            stripped = ln.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            if stripped.startswith(("def ", "async def ", "class ", "@")):
                lines.append(ln.rstrip())
                continue
            if re.match(
                r"^(public|private|protected|func|type|interface|class|enum)\b",
                stripped,
            ):
                lines.append(ln.rstrip())
                continue
            if stripped.endswith(("{", ";")) and "(" in stripped:
                lines.append(ln.rstrip())
        return "\n".join(lines).strip()

    @classmethod
    def build_embed_queries(
        cls,
        code_text: str,
        *,
        lexicon: Optional[LexiconExpandable] = None,
    ) -> List[str]:
        raw = (code_text or "").strip()
        if not raw:
            return []
        if NlToCodeEnhancement.is_enabled() and NlCodeQueryBuilder.looks_like_nl(raw):
            return NlCodeQueryBuilder.build_embed_queries(raw, lexicon=lexicon)
        normalized = cls.normalize(raw)
        signatures = cls.signature_lines(raw)
        queries: List[str] = []
        seen: Set[str] = set()

        def _add(text: str) -> None:
            key = text.strip()
            if not key or key in seen:
                return
            seen.add(key)
            queries.append(key)

        _add(raw)
        if normalized and normalized != raw:
            _add(normalized)
        if signatures and len(signatures) >= 8:
            _add(signatures)
        return queries
