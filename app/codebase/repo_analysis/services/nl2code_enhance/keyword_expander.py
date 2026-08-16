"""related 关键词扩展：从 query 抽 token，并对拉丁标识做形态变换。"""
from __future__ import annotations
import re
from typing import List, Optional, Protocol, Sequence, Set, runtime_checkable


@runtime_checkable
class LexiconExpandable(Protocol):
    def expand_tokens(self, tokens: Sequence[str], *, limit: int = 16) -> List[str]:
        ...


class RelatedKeywordExpander:
    """NL/关键词 → 可 grep/exact/路径对齐的 token（标识符形态变换）。"""

    TOKEN_STOP: Set[str] = {
        "for",
        "the",
        "and",
        "with",
        "from",
        "into",
        "that",
        "this",
        "agent",
        "default",
        "prompt",
        "where",
        "what",
        "how",
        "find",
        "查找",
        "相关",
        "实现",
        "位置",
        "在哪",
        "哪里",
    }

    GENERIC_TOKENS: Set[str] = {
        "manager",
        "service",
        "base",
        "provider",
        "client",
        "handler",
        "message",
        "messages",
        "connection",
        "process",
        "common",
        "utils",
        "util",
        "core",
        "data",
        "channel",
    }

    _TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}")
    _IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    @classmethod
    def expand(
        cls,
        keywords: Sequence[str],
        *,
        lexicon: Optional[LexiconExpandable] = None,
    ) -> List[str]:
        """展开：原词 + core_tokens + 拉丁标识形态；可选仓内 lexicon 对齐扩展。"""
        out: List[str] = []
        seen: set[str] = set()

        def _add(token: str) -> None:
            t = (token or "").strip()
            if not t:
                return
            key = t.casefold()
            if key in seen:
                return
            seen.add(key)
            out.append(t)

        seeds = list(keywords or [])
        for raw in list(seeds):
            for tok in cls.core_tokens(str(raw or "")):
                if tok not in seeds:
                    seeds.append(tok)

        for raw in seeds:
            kw = str(raw or "").strip()
            if not kw:
                continue
            _add(kw)
            if len(kw) > 40:
                continue
            folded = kw.casefold()
            if folded in cls.GENERIC_TOKENS or folded in cls.TOKEN_STOP:
                continue
            if kw.isascii() and cls._IDENT_RE.fullmatch(kw):
                for v in cls.morph_variants(kw):
                    _add(v)

        if lexicon is not None:
            for hit in lexicon.expand_tokens(out):
                _add(str(hit))
        return out

    @classmethod
    def morph_variants(cls, token: str) -> List[str]:
        """标识符形态：camel ↔ snake，以及拆出的有意义片段（≥3）。"""
        name = (token or "").strip()
        if not name or not name.isascii():
            return []
        out: List[str] = []
        snake = cls.to_snake(name)
        if snake and snake.casefold() != name.casefold():
            out.append(snake)
        camel = cls.to_camel(snake or name)
        if camel and camel.casefold() != name.casefold():
            out.append(camel)
        parts = [p for p in re.split(r"[_\-]+", snake or name) if len(p) >= 3]
        if len(parts) >= 2:
            for p in parts:
                if p.casefold() not in cls.GENERIC_TOKENS:
                    out.append(p)
        return out

    @classmethod
    def to_snake(cls, name: str) -> str:
        text = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip())
        text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
        return re.sub(r"_+", "_", text).strip("_").lower()

    @classmethod
    def to_camel(cls, name: str) -> str:
        parts = [p for p in re.split(r"[_\-]+", (name or "").strip()) if p]
        if not parts:
            return ""
        return parts[0].lower() + "".join(p[:1].upper() + p[1:].lower() for p in parts[1:])

    @classmethod
    def core_tokens(cls, text: str) -> List[str]:
        """从自然语言 query 抽可对齐路径/符号的核心词。"""
        out: List[str] = []
        seen: Set[str] = set()
        for m in cls._TOKEN_RE.finditer(text or ""):
            tok = m.group(0).strip("-_")
            if not tok:
                continue
            key = tok.casefold()
            if key in cls.TOKEN_STOP or key in seen:
                continue
            if len(tok) < 2:
                continue
            seen.add(key)
            out.append(tok)
            if len(out) >= 12:
                break
        specific = [t for t in out if t.casefold() not in cls.GENERIC_TOKENS]
        return specific if specific else out

    @classmethod
    def prioritize_for_grep(cls, terms: Sequence[str]) -> List[str]:
        """grep 词序：标识符优先，丢弃纯停用/泛化弱词。"""
        idents: List[str] = []
        phrases: List[str] = []
        seen: set[str] = set()

        def _add(bucket: List[str], token: str) -> None:
            t = (token or "").strip()
            if not t:
                return
            key = t.casefold()
            if key in seen:
                return
            seen.add(key)
            bucket.append(t)

        for raw in terms or []:
            t = str(raw or "").strip()
            if not t:
                continue
            folded = t.casefold()
            if folded in cls.TOKEN_STOP or folded in cls.GENERIC_TOKENS:
                continue
            if len(t) > 40:
                _add(phrases, t)
            elif t.isascii() and cls._IDENT_RE.fullmatch(t):
                _add(idents, t)
            elif not t.isascii():
                continue
            else:
                _add(phrases, t)

        def _ident_rank(name: str) -> tuple:
            camel = any(c.isupper() for c in name[1:])
            snake = "_" in name
            return (0 if (camel or snake) else 1, -len(name), name.casefold())

        idents.sort(key=_ident_rank)
        return idents + phrases

    @classmethod
    def path_matches_tokens(
        cls,
        file_path: str,
        keywords: Sequence[str],
        *,
        lexicon: Optional[LexiconExpandable] = None,
    ) -> bool:
        """路径是否命中 expand 后的拉丁 token（用于 NL resolve 排序加权）。"""
        fp = (file_path or "").replace("\\", "/").lower()
        if not fp:
            return False
        stem = fp.rsplit("/", 1)[-1]
        stem_base = stem.rsplit(".", 1)[0]
        for token in cls.expand(keywords or [], lexicon=lexicon):
            a = str(token or "").strip().lower()
            if not a.isascii() or len(a) < 4:
                continue
            if a in cls.TOKEN_STOP or a in cls.GENERIC_TOKENS:
                continue
            compact = a.replace("_", "")
            if a in fp or a in stem_base or compact in stem_base.replace("_", ""):
                return True
        return False

    @classmethod
    def is_noise_path(cls, file_path: str) -> bool:
        from ..codegraph.graph_result_normalizer import GraphResultNormalizer

        return GraphResultNormalizer.is_noise_path(file_path)
