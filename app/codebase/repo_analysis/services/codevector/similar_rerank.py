from __future__ import annotations
import re
from typing import Dict, List, Set
from .similar_query import SimilarQueryNormalizer


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


class SimilarRerankService:
    """similar 向量召回后的 lexical + 符号 + 稀有特征 + 路径加权 rerank。"""

    VECTOR_WEIGHT = 0.42
    LEXICAL_WEIGHT = 0.26
    SYMBOL_WEIGHT = 0.10
    RARE_WEIGHT = 0.12
    PATH_WEIGHT = 0.10

    @classmethod
    def rerank(
        cls,
        docs: List[Dict[str, object]],
        query_text: str,
        symbol_names: Set[str],
    ) -> List[Dict[str, object]]:
        if not docs:
            return []
        normalized_query = SimilarQueryNormalizer.normalize(query_text)
        query_tokens = cls._tokenize(normalized_query)
        rare_tokens = cls._rare_tokens(query_tokens | {n for n in symbol_names if n})
        ranked: List[Dict[str, object]] = []
        for doc in docs:
            content = str(doc.get("content") or "")
            file_path = str(doc.get("file_path") or "")
            content_tokens = cls._tokenize(content)
            vec_score = cls._as_float(doc.get("_score"))
            lex_score = cls._lexical_overlap(query_tokens, content_tokens)
            sym_score = cls._symbol_overlap(symbol_names, content)
            rare_score = cls._rare_overlap(rare_tokens, content_tokens)
            # 路径分：查询稀有词 + 候选内容稀有词（避免 session↔sessions 等泛匹配反客为主）
            path_score = max(
                cls._path_overlap(rare_tokens, file_path),
                cls._path_overlap(cls._rare_tokens(content_tokens), file_path),
            )
            fused = (
                vec_score * cls.VECTOR_WEIGHT
                + lex_score * cls.LEXICAL_WEIGHT
                + sym_score * cls.SYMBOL_WEIGHT
                + rare_score * cls.RARE_WEIGHT
                + path_score * cls.PATH_WEIGHT
            )
            item = dict(doc)
            item["_fused_score"] = fused
            item["_lexical_score"] = lex_score
            item["_symbol_score"] = sym_score
            item["_rare_score"] = rare_score
            item["_path_score"] = path_score
            ranked.append(item)
        ranked.sort(
            key=lambda it: (
                -float(it.get("_fused_score") or 0),
                -float(it.get("_rare_score") or 0),
                -float(it.get("_path_score") or 0),
                -float(it.get("_score") or 0),
                str(it.get("file_path") or ""),
            )
        )
        return cls._dedupe_by_file(ranked)

    @classmethod
    def _rare_tokens(cls, tokens: Set[str]) -> Set[str]:
        rare: Set[str] = set()
        for tok in tokens:
            t = (tok or "").strip()
            if len(t) < 4:
                continue
            if "_" in t or len(t) >= 6:
                rare.add(t)
        return rare

    @classmethod
    def _rare_overlap(cls, rare_tokens: Set[str], candidate_tokens: Set[str]) -> float:
        if not rare_tokens:
            return 0.0
        if not candidate_tokens:
            return 0.0
        cand_lower = {c.lower() for c in candidate_tokens}
        hits = 0
        for tok in rare_tokens:
            low = tok.lower()
            if low in cand_lower:
                hits += 1
                continue
            # 子串命中：改写后仍保留 jwks / SESSION_MANAGER 等片段
            if any(low in c or c in low for c in cand_lower if len(c) >= 4):
                hits += 1
        return min(1.0, hits / max(len(rare_tokens), 1))

    @classmethod
    def _path_overlap(cls, tokens: Set[str], file_path: str) -> float:
        if not tokens or not file_path:
            return 0.0
        path = file_path.replace("\\", "/").lower()
        parts = [p for p in re.split(r"[/_.\-]+", path) if p]
        if not parts:
            return 0.0
        part_set = set(parts)
        hits = 0
        weighed = 0.0
        for tok in tokens:
            low = tok.lower()
            if len(low) < 4:
                continue
            weighed += 1.0
            if cls._token_hits_path(low, part_set, parts):
                hits += 1
        if weighed <= 0:
            return 0.0
        return min(1.0, hits / weighed)

    @staticmethod
    def _token_hits_path(low: str, part_set: Set[str], parts: List[str]) -> bool:
        """路径命中：优先整段相等；复合词按 _ 拆段精确对齐，避免 session↔sessions 误伤。"""
        if low in part_set:
            return True
        if len(low) >= 8 and any(low in p for p in parts):
            return True
        for seg in low.split("_"):
            if len(seg) >= 4 and seg in part_set:
                return True
        return False

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        return set(_TOKEN.findall(text or ""))

    @staticmethod
    def _lexical_overlap(query_tokens: Set[str], candidate_tokens: Set[str]) -> float:
        if not query_tokens:
            return 0.0
        if not candidate_tokens:
            return 0.0
        return len(query_tokens & candidate_tokens) / len(query_tokens)

    @staticmethod
    def _symbol_overlap(symbol_names: Set[str], content: str) -> float:
        if not symbol_names or not content:
            return 0.0
        hits = sum(1 for name in symbol_names if name in content)
        return min(1.0, hits / max(len(symbol_names), 1))

    @staticmethod
    def _dedupe_by_file(items: List[Dict[str, object]]) -> List[Dict[str, object]]:
        best: Dict[str, Dict[str, object]] = {}
        for it in items:
            fp = str(it.get("file_path") or "")
            if not fp:
                continue
            prev = best.get(fp)
            if prev is None or float(it.get("_fused_score") or 0) > float(prev.get("_fused_score") or 0):
                best[fp] = it
        return sorted(
            best.values(),
            key=lambda it: (
                -float(it.get("_fused_score") or 0),
                -float(it.get("_rare_score") or 0),
                -float(it.get("_path_score") or 0),
                str(it.get("file_path") or ""),
            ),
        )

    @staticmethod
    def _as_float(value: object) -> float:
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
