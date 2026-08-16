"""NL → Code 查询构造：弱自然语言转可对齐源码向量的多视角 embed 文本。"""
from __future__ import annotations
import re
from typing import List, Optional, Set
from .keyword_expander import (
    LexiconExpandable,
    RelatedKeywordExpander,
)


class NlCodeQueryBuilder:
    """NL → 多视角 embedding 文本：原句 + instruct + 拉丁标识形态/HyDE。"""

    CODE_RETRIEVAL_INSTRUCT = (
        "Instruct: Given a code search query, retrieve relevant source code "
        "that implements the described functionality.\nQuery: "
    )

    _CN = re.compile(r"[\u4e00-\u9fff]")
    _CODE_HINT = re.compile(
        r"(?m)^\s*(?:async\s+def|def|class|func|import|from\s+\w+\s+import|"
        r"public|private|protected|package|using|#\s*include|"
        r"fn|let|const|var|interface|type|struct)\b"
        r"|[{};]\s*$|=>\s*\{?"
    )
    _IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _MAX_QUERIES = 8
    _MAX_IDENTS_IN_JOIN = 12
    _MAX_HYDE = 3

    @classmethod
    def looks_like_nl(cls, text: str) -> bool:
        """判断是否应按 NL→Code 路径处理（非代码片段、非短标识符）。"""
        q = (text or "").strip()
        if not q:
            return False
        if cls._looks_like_code(q):
            return False
        if cls._CN.search(q):
            return True
        if "\n" in q or len(q) > 160:
            return False
        if cls._IDENT_RE.fullmatch(q):
            return False
        parts = [p for p in re.split(r"\s+", q) if p]
        if len(parts) >= 2:
            return True
        tokens = RelatedKeywordExpander.core_tokens(q)
        return len(tokens) >= 2

    @classmethod
    def _looks_like_code(cls, query: str) -> bool:
        lines = [ln for ln in query.splitlines() if ln.strip()]
        if len(lines) >= 2 and cls._CODE_HINT.search(query):
            return True
        if len(query) >= 40 and cls._CODE_HINT.search(query):
            return True
        return False

    @classmethod
    def build_embed_queries(
        cls,
        query: str,
        *,
        lexicon: Optional[LexiconExpandable] = None,
    ) -> List[str]:
        raw = (query or "").strip()
        if not raw:
            return []
        expanded = RelatedKeywordExpander.expand([raw], lexicon=lexicon)
        latin_idents = [
            t
            for t in expanded
            if t.isascii()
            and re.search(r"[A-Za-z]", t)
            and t.casefold() != raw.casefold()
            and t.casefold() not in RelatedKeywordExpander.TOKEN_STOP
            and t.casefold() not in RelatedKeywordExpander.GENERIC_TOKENS
        ]
        out: List[str] = []
        seen: Set[str] = set()

        def _add(text: str) -> None:
            key = (text or "").strip()
            if not key or key in seen:
                return
            seen.add(key)
            out.append(key)

        _add(raw)
        _add(cls.CODE_RETRIEVAL_INSTRUCT + raw)
        if latin_idents:
            joined = " ".join(latin_idents[: cls._MAX_IDENTS_IN_JOIN])
            _add(joined)
            _add(cls.CODE_RETRIEVAL_INSTRUCT + joined)
            for ident in latin_idents[: cls._MAX_HYDE]:
                for snip in cls.build_hyde_snippets(ident):
                    _add(snip)
                    if len(out) >= cls._MAX_QUERIES:
                        return out[: cls._MAX_QUERIES]
        return out[: cls._MAX_QUERIES]

    @classmethod
    def build_hyde_snippets(cls, ident: str) -> List[str]:
        """无 LLM 的轻量多语言 HyDE：Python / JS / 通用签名。"""
        name = (ident or "").strip()
        if not name:
            return []
        snake = RelatedKeywordExpander.to_snake(name) or "handler"
        if snake[0].isdigit():
            snake = f"fn_{snake}"
        snake = snake[:64]
        return [
            (
                f"# {name} implementation\n"
                f"def {snake}(self, *args, **kwargs):\n"
                f'    """Handle {name} for the application."""\n'
                f"    pass\n"
            ),
            (
                f"// {name} implementation\n"
                f"function {snake}(...args) {{\n"
                f"  // Handle {name}\n"
                f"}}\n"
            ),
            (
                f"// {name}\n"
                f"{snake}(/* args */) {{ /* Handle {name} */ }}\n"
            ),
        ]

    @classmethod
    def build_hyde_snippet(cls, ident: str) -> str:
        """兼容旧调用：返回 Python 主片段。"""
        snips = cls.build_hyde_snippets(ident)
        return snips[0] if snips else ""
