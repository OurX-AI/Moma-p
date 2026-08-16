"""NL→Code 查询准备门面：统一 lexicon / 改写 / embed 与关键词种子，避免重复打 LLM。"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Sequence
from .gate import NlToCodeEnhancement
from .keyword_expander import RelatedKeywordExpander
from .lexicon import RepoIdentifierLexicon
from .query_builder import NlCodeQueryBuilder
from .rewriter import NlQueryRewriter, NlRewriteResult

RewriteMode = Literal["auto", "force", "skip"]


@dataclass
class NlQueryPrepResult:
    """一次准备的结果：原查询保留，改写仅作为额外种子。"""

    original_query: str
    keywords: List[str] = field(default_factory=list)
    embed_queries: List[str] = field(default_factory=list)
    rewrite: Optional[NlRewriteResult] = None
    rewrite_trigger: Optional[str] = None
    lexicon: Optional[RepoIdentifierLexicon] = None

    def meta(self) -> Optional[dict]:
        if self.rewrite is None:
            return None
        return {
            "english": self.rewrite.english_query,
            "identifiers": list(self.rewrite.identifiers),
            "trigger": self.rewrite_trigger,
        }


class NlQueryPrep:
    """统一 Prep：SearchService / ResolveService 共用，改写至多一次。"""

    @classmethod
    async def prepare(
        cls,
        repo_id: str,
        query: str,
        *,
        keywords: Optional[Sequence[str]] = None,
        rewrite: RewriteMode = "auto",
        rewrite_result: Optional[NlRewriteResult] = None,
        rewrite_trigger: Optional[str] = None,
    ) -> NlQueryPrepResult:
        # 延迟导入，避免与 similar_query ↔ nl2code_enhance 包初始化环依赖
        from ..codevector.similar_query import SimilarQueryNormalizer

        original = (query or "").strip()
        seeds: List[str] = []
        seen_seed: set[str] = set()

        def _add_seed(text: str) -> None:
            t = (text or "").strip()
            if not t:
                return
            key = t.casefold()
            if key in seen_seed:
                return
            seen_seed.add(key)
            seeds.append(t)

        if keywords:
            for k in keywords:
                _add_seed(str(k))
        _add_seed(original)

        lexicon = await NlToCodeEnhancement.lexicon_for_repo(repo_id)
        resolved_rewrite = rewrite_result
        trigger = rewrite_trigger

        if resolved_rewrite is None and rewrite != "skip":
            should = False
            if rewrite == "force":
                should = NlQueryRewriter.is_enabled() and NlCodeQueryBuilder.looks_like_nl(original)
            elif rewrite == "auto":
                should = NlQueryRewriter.should_rewrite_upfront(original)
            if should and original:
                resolved_rewrite = await NlQueryRewriter.rewrite(original)
                if resolved_rewrite and resolved_rewrite.seeds():
                    if rewrite_trigger:
                        trigger = rewrite_trigger
                    elif rewrite == "force":
                        trigger = "weak"
                    else:
                        trigger = "always"
                else:
                    resolved_rewrite = None
                    trigger = None

        if resolved_rewrite is not None:
            for seed in resolved_rewrite.seeds():
                _add_seed(seed)

        expanded = RelatedKeywordExpander.expand(seeds, lexicon=lexicon)
        embed_queries = SimilarQueryNormalizer.build_embed_queries(original, lexicon=lexicon)
        if resolved_rewrite is not None:
            for seed in resolved_rewrite.seeds():
                for q in NlCodeQueryBuilder.build_embed_queries(seed, lexicon=lexicon):
                    if q not in embed_queries:
                        embed_queries.append(q)

        return NlQueryPrepResult(
            original_query=original,
            keywords=expanded,
            embed_queries=embed_queries,
            rewrite=resolved_rewrite,
            rewrite_trigger=trigger,
            lexicon=lexicon,
        )
