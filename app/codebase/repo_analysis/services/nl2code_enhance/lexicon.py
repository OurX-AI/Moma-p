"""仓内标识词表：从已索引路径（及可选符号名）抽取，供 NL expand 模糊对齐。"""
from __future__ import annotations
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set
from .keyword_expander import RelatedKeywordExpander


class RepoIdentifierLexicon:
    """该仓里出现过的拉丁标识集合（非同义词表）。"""

    MAX_EXPAND = 16
    MIN_TOKEN_LEN = 4
    _PREFIX_KEY_LEN = 3
    _IDENT_CHUNK = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
    _cache: Dict[str, "RepoIdentifierLexicon"] = {}

    def __init__(self, identifiers: Iterable[str]):
        self._idents: List[str] = []
        self._fold: Set[str] = set()
        self._by_fold: Dict[str, str] = {}
        self._prefix_buckets: Dict[str, List[str]] = {}
        for raw in identifiers or []:
            name = str(raw or "").strip()
            if not name or not name.isascii():
                continue
            if not self._IDENT_CHUNK.fullmatch(name):
                continue
            folded = name.casefold()
            if folded in self._fold:
                continue
            if folded in RelatedKeywordExpander.TOKEN_STOP:
                continue
            if folded in RelatedKeywordExpander.GENERIC_TOKENS:
                continue
            self._fold.add(folded)
            self._idents.append(name)
            self._by_fold[folded] = name
            compact = folded.replace("_", "")
            key_srcs = [folded, compact]
            key_srcs.extend(p for p in re.split(r"[_\-]+", folded) if p)
            for key_src in key_srcs:
                if len(key_src) < self._PREFIX_KEY_LEN:
                    continue
                bucket = key_src[: self._PREFIX_KEY_LEN]
                bucket_list = self._prefix_buckets.setdefault(bucket, [])
                if name not in bucket_list:
                    bucket_list.append(name)

    @property
    def size(self) -> int:
        return len(self._idents)

    @classmethod
    def from_paths(cls, paths: Sequence[str]) -> "RepoIdentifierLexicon":
        ids: List[str] = []
        for raw in paths or []:
            fp = str(raw or "").replace("\\", "/").strip()
            if not fp:
                continue
            stem = fp.rsplit("/", 1)[-1]
            stem_base = stem.rsplit(".", 1)[0]
            for part in fp.split("/"):
                part = part.strip()
                if not part or "." in part:
                    continue
                ids.extend(cls._split_path_token(part))
            ids.extend(cls._split_path_token(stem_base))
        return cls(ids)

    @classmethod
    def from_paths_and_symbols(
        cls,
        paths: Sequence[str],
        symbols: Optional[Sequence[str]] = None,
    ) -> "RepoIdentifierLexicon":
        ids = list(cls.from_paths(paths)._idents)
        for sym in symbols or []:
            name = str(sym or "").strip()
            if not name:
                continue
            for piece in name.replace("::", ".").split("."):
                ids.extend(cls._split_path_token(piece))
        return cls(ids)

    @classmethod
    def _split_path_token(cls, token: str) -> List[str]:
        t = (token or "").strip()
        if not t or not t.isascii():
            return []
        out = [t] if cls._IDENT_CHUNK.fullmatch(t) else []
        snake = RelatedKeywordExpander.to_snake(t)
        if snake and snake != t.casefold():
            out.append(snake)
        for m in cls._IDENT_CHUNK.finditer(t):
            out.append(m.group(0))
        for part in re.split(r"[_\-]+", snake or t):
            if len(part) >= 3:
                out.append(part)
        return out

    def _candidates_for_seed(self, seed_fold: str, compact_seed: str) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()

        def _add(name: str) -> None:
            key = name.casefold()
            if key in seen:
                return
            seen.add(key)
            out.append(name)

        exact = self._by_fold.get(seed_fold)
        if exact:
            _add(exact)
        for key_src in (seed_fold, compact_seed):
            if len(key_src) < self._PREFIX_KEY_LEN:
                continue
            for name in self._prefix_buckets.get(key_src[: self._PREFIX_KEY_LEN], []):
                _add(name)
        return out

    def expand_tokens(self, tokens: Sequence[str], *, limit: int = MAX_EXPAND) -> List[str]:
        """用 query 中的拉丁 token 在词表里找相关标识（精确/前缀/包含）。"""
        out: List[str] = []
        seen: Set[str] = set()

        def _add(name: str) -> None:
            key = name.casefold()
            if key in seen:
                return
            seen.add(key)
            out.append(name)

        seeds = [str(t).strip() for t in (tokens or []) if str(t).strip()]
        latin_seeds: List[str] = []
        for s in seeds:
            if not s.isascii():
                continue
            if len(s) < self.MIN_TOKEN_LEN:
                continue
            folded = s.casefold()
            if folded in RelatedKeywordExpander.TOKEN_STOP or folded in RelatedKeywordExpander.GENERIC_TOKENS:
                continue
            latin_seeds.append(s)
            for v in RelatedKeywordExpander.morph_variants(s):
                if v.casefold() not in {x.casefold() for x in latin_seeds}:
                    latin_seeds.append(v)

        for seed in latin_seeds:
            sf = seed.casefold()
            compact_seed = sf.replace("_", "")
            for ident in self._candidates_for_seed(sf, compact_seed):
                if len(out) >= limit:
                    return out
                inf = ident.casefold()
                compact_id = inf.replace("_", "")
                if inf == sf or compact_id == compact_seed:
                    _add(ident)
                    continue
                # 仅 ident 以 seed 为前缀，避免短 ident 反向污染长 seed
                if len(sf) >= self.MIN_TOKEN_LEN and (
                    inf.startswith(sf) or compact_id.startswith(compact_seed)
                ):
                    _add(ident)
                    continue
                if len(sf) >= 5 and (sf in inf or compact_seed in compact_id):
                    _add(ident)
        return out

    @classmethod
    def cache_get(cls, repo_id: str) -> Optional["RepoIdentifierLexicon"]:
        return cls._cache.get(repo_id)

    @classmethod
    def cache_put(cls, repo_id: str, lexicon: "RepoIdentifierLexicon") -> "RepoIdentifierLexicon":
        cls._cache[repo_id] = lexicon
        return lexicon

    @classmethod
    def cache_clear(cls, repo_id: Optional[str] = None) -> None:
        if repo_id is None:
            cls._cache.clear()
        else:
            cls._cache.pop(repo_id, None)

    @classmethod
    def invalidate_repo(cls, repo_id: str) -> None:
        """analyze / 删文件后使词表缓存失效。"""
        rid = (repo_id or "").strip()
        if rid:
            cls.cache_clear(rid)

    @classmethod
    async def for_repo(cls, repo_id: str, *, use_cache: bool = True) -> "RepoIdentifierLexicon":
        if use_cache:
            hit = cls.cache_get(repo_id)
            if hit is not None:
                return hit
        from ..codevector.exact_match import ExactMatchService

        paths = await ExactMatchService.list_indexed_file_paths(repo_id)
        symbols = await ExactMatchService.list_indexed_symbol_names(repo_id)
        lex = cls.from_paths_and_symbols(paths, symbols)
        if use_cache:
            cls.cache_put(repo_id, lex)
        return lex
