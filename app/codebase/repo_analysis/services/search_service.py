from __future__ import annotations
import logging
from typing import Dict, List, Optional, Set, Tuple
from sqlalchemy import select
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from .codevector.exact_match import ExactMatchService
from .nl2code_enhance import (
    NlQueryPrep,
    NlQueryPrepResult,
    RelatedKeywordExpander,
)
from .codevector.similar_query import SimilarQueryNormalizer
from .codevector.similar_rerank import SimilarRerankService
from .codevector.vector_search import CodeVectorSearchService
from .mr_experience.pattern_vector import PatternVectorService
from .search_index_meta import SearchIndexMeta
from .dir_sibling_expander import DirSiblingExpander
from ...repo_mgmt.models.git_repo_mgmt import GitRepository, RepoKind


class SearchService:
    """仓库内代码相似检索、关键词关联位置检索、历史经验模式检索。"""

    # ==================== related（关键词关联位置检索）配置 ====================
    # 精确符号匹配的加分系数（symbol_score >= 2.8 时使用）
    EXACT_SYMBOL_BOOST = 1.2
    # 弱精确符号匹配的加分系数（symbol_score < 2.8 时使用）
    EXACT_SYMBOL_WEAK_BOOST = 0.6
    # 路径精确匹配的加分系数
    EXACT_PATH_BOOST = 0.3
    # 代码图谱（callers/callees）的默认分数
    CODEGRAPH_SCORE = 1.0
    # related 结果相对 top1 的分数门槛（低于此比例的结果被过滤）
    SCORE_RATIO_FLOOR = 0.55
    # 无强 exact 命中时的弱语义短列表配置（抬高 Precision，保 Top1）
    RELATED_WEAK_SCORE_RATIO = 0.72  # 弱语义分数门槛
    RELATED_WEAK_CAP = 5             # 弱语义最多返回条数
    RELATED_WEAK_DIR_QUOTA = 2       # 弱语义每个目录最多返回条数
    # 存在强符号定义命中时的配置（主列表更短，其余进 also_consider）
    STRONG_SYMBOL_CAP = 3            # 强符号最多返回条数
    STRONG_SYMBOL_RAW = 2.8          # 强符号的原始分数阈值
    # also_consider（补充候选）配置
    RELATED_ALSO_CONSIDER_CAP = 8    # also_consider 最多返回条数
    RELATED_ALSO_SCORE_RATIO = 0.55  # also_consider 相对 top1 的分数门槛
    # 给用户的读取提示
    READ_HINT = "优先读 items；改代码前扫 also_consider，防漏相关文件"
    # ==================== similar（相似代码检索）配置 ====================
    # 向量检索时的倍数因子：fetch_k = max(top_k * SIMILAR_FETCH_MULTIPLIER, SIMILAR_MIN_FETCH)
    SIMILAR_FETCH_MULTIPLIER = 4
    # 向量检索时的最小召回数量（即使 top_k 很小，也至少召回这么多）
    SIMILAR_MIN_FETCH = 40
    # 相似结果相对 top1 的分数门槛（低于此比例的结果被过滤，越高越严格）
    SIMILAR_SCORE_RATIO_FLOOR = 0.82
    # 相似结果的最大返回条数（根据信号强度动态调整）
    SIMILAR_SOFT_CAP = 10           # 弱信号时最多返回条数
    SIMILAR_STRONG_CAP = 5          # 强信号时最多返回条数
    SIMILAR_VERY_STRONG_CAP = 3     # 极强信号时最多返回条数
    # 相似结果中每个目录最多返回条数（防止结果集中在同一目录）
    SIMILAR_DIR_QUOTA = 3
    # 相似信号强度判断阈值
    SIMILAR_STRONG_SYMBOL_SCORE = 0.5      # 强符号匹配阈值（symbol_score >= 0.5）
    SIMILAR_STRONG_LEXICAL_SCORE = 0.35    # 强词汇匹配阈值（lexical_score >= 0.35）
    SIMILAR_VERY_STRONG_SYMBOL_SCORE = 0.8 # 极强符号匹配阈值（symbol_score >= 0.8）

    @classmethod
    def _merge_similar_docs(
        cls,
        batches: List[List[Dict[str, object]]],
    ) -> List[Dict[str, object]]:
        best: Dict[Tuple[object, object, object], Dict[str, object]] = {}
        for docs in batches:
            for doc in docs:
                key = (
                    doc.get("file_path"),
                    doc.get("start_line"),
                    doc.get("end_line"),
                )
                score = float(doc.get("_score") or 0)
                prev = best.get(key)
                if prev is None or score > float(prev.get("_score") or 0):
                    best[key] = doc
        return list(best.values())

    @classmethod
    def _similar_parent_dir(cls, file_path: object) -> str:
        fp = str(file_path or "").replace("\\", "/")
        if "/" not in fp:
            return ""
        return fp.rsplit("/", 1)[0]

    @classmethod
    def _has_similar_strong_signal(
        cls,
        items: List[Dict[str, object]],
        symbol_names: Set[str],
    ) -> bool:
        return cls._similar_signal_level(items, symbol_names) >= 1

    @classmethod
    def _similar_signal_level(
        cls,
        items: List[Dict[str, object]],
        symbol_names: Set[str],
    ) -> int:
        """0=弱，1=强，2=极强（宜只返回 1 条）。"""
        if not items:
            return 0
        top = items[0]
        sym = float(top.get("symbol_score") or 0)
        lex = float(top.get("lexical_score") or 0)
        top_path = str(top.get("file_path") or "").replace("\\", "/")
        stem = top_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        path_hit = False
        for name in symbol_names:
            n = str(name or "").strip()
            if not n:
                continue
            if n.lower() == stem or n.lower() in top_path.lower():
                path_hit = True
                break
        if path_hit or sym >= cls.SIMILAR_VERY_STRONG_SYMBOL_SCORE:
            return 2
        if sym >= cls.SIMILAR_STRONG_SYMBOL_SCORE and lex >= cls.SIMILAR_STRONG_LEXICAL_SCORE:
            return 1
        return 0

    @classmethod
    def _apply_dir_quota(
        cls,
        items: List[Dict[str, object]],
        quota: int,
    ) -> List[Dict[str, object]]:
        if quota <= 0:
            return list(items)
        kept: List[Dict[str, object]] = []
        counts: Dict[str, int] = {}
        for it in items:
            parent = cls._similar_parent_dir(it.get("file_path"))
            used = counts.get(parent, 0)
            if used >= quota:
                continue
            counts[parent] = used + 1
            kept.append(it)
        return kept

    @classmethod
    def _apply_similar_trim(
        cls,
        items: List[Dict[str, object]],
        top_k: int,
        *,
        symbol_names: Optional[Set[str]] = None,
    ) -> List[Dict[str, object]]:
        if not items:
            return []
        names = symbol_names or set()
        top_score = float(items[0].get("score") or 0)
        floor = top_score * cls.SIMILAR_SCORE_RATIO_FLOOR
        trimmed = [it for it in items if float(it.get("score") or 0) >= floor]
        if not trimmed:
            trimmed = items[:1]
        trimmed = cls._apply_dir_quota(trimmed, cls.SIMILAR_DIR_QUOTA)
        level = cls._similar_signal_level(trimmed, names)
        if level >= 2:
            soft_cap = cls.SIMILAR_VERY_STRONG_CAP
        elif level == 1:
            soft_cap = cls.SIMILAR_STRONG_CAP
        else:
            soft_cap = cls.SIMILAR_SOFT_CAP
        cap = min(max(1, top_k), soft_cap)
        return trimmed[:cap]

    @classmethod
    def fuse_similar_items(
        cls,
        docs: List[Dict[str, object]],
        *,
        query_text: str,
        top_k: int,
    ) -> List[Dict[str, object]]:
        symbol_names = SimilarQueryNormalizer.extract_symbol_names(query_text)
        reranked = SimilarRerankService.rerank(docs, query_text, symbol_names)
        items: List[Dict[str, object]] = []
        for doc in reranked:
            items.append(
                {
                    "file_path": doc.get("file_path"),
                    "start_line": doc.get("start_line"),
                    "end_line": doc.get("end_line"),
                    "score": doc.get("_fused_score"),
                    "vector_score": doc.get("_score"),
                    "lexical_score": doc.get("_lexical_score"),
                    "symbol_score": doc.get("_symbol_score"),
                    "match_source": "line_chunk",
                }
            )
        return cls._apply_similar_trim(items, top_k, symbol_names=symbol_names)

    @staticmethod
    def _item_key(it: Dict[str, object]) -> Tuple[object, object, object, object, object]:
        return (
            it.get("file_path"),
            it.get("symbol_kind"),
            it.get("symbol_name"),
            it.get("start_line"),
            it.get("end_line"),
        )

    @classmethod
    def _normalize_vector_item(cls, doc: Dict[str, object], source: str) -> Dict[str, object]:
        score = doc.get("_score")
        try:
            score_f = float(score) if score is not None else 0.5
        except (TypeError, ValueError):
            score_f = 0.5
        return {
            "file_path": doc.get("file_path"),
            "symbol_kind": doc.get("symbol_kind"),
            "symbol_name": doc.get("symbol_name"),
            "start_line": doc.get("start_line"),
            "end_line": doc.get("end_line"),
            "score": score_f,
            "match_source": source,
            "exact_tier": None,
        }

    @classmethod
    def _exact_boost(cls, tier: str) -> float:
        if tier == "symbol":
            return cls.EXACT_SYMBOL_BOOST
        if tier == "symbol_weak":
            return cls.EXACT_SYMBOL_WEAK_BOOST
        return cls.EXACT_PATH_BOOST

    @classmethod
    def _rank_key(cls, item: Dict[str, object]) -> Tuple[int, float, str]:
        source = str(item.get("match_source") or "")
        tier = str(item.get("exact_tier") or "")
        score = float(item.get("score") or 0)
        if source == "exact" and tier == "symbol":
            group = 0
        elif source == "exact" and tier == "symbol_weak":
            group = 1
        elif source == "exact":
            group = 2
        elif source == "symbol_summary":
            group = 3
        elif source == "grep":
            group = 4
        elif source == "line_chunk":
            group = 5
        elif source == "mr_experience":
            group = 6
        elif source == "codegraph":
            group = 7
        else:
            group = 8
        return (group, -score, str(item.get("file_path") or ""))

    @classmethod
    def _has_strong_symbol(cls, items: List[Dict[str, object]]) -> bool:
        for it in items:
            if str(it.get("match_source") or "") != "exact":
                continue
            if str(it.get("exact_tier") or "") != "symbol":
                continue
            raw = float(it.get("_raw_score") or 0)
            if raw >= cls.STRONG_SYMBOL_RAW:
                return True
            score = float(it.get("score") or 0)
            if score >= cls.STRONG_SYMBOL_RAW + cls.EXACT_SYMBOL_BOOST - 1e-6:
                return True
        return False

    @classmethod
    def _file_key(cls, it: Dict[str, object]) -> str:
        return str(it.get("file_path") or "")

    @classmethod
    def _apply_precision_trim(
        cls,
        unique: List[Dict[str, object]],
        top_k: int,
    ) -> List[Dict[str, object]]:
        primary, _also = cls._split_precision_layers(unique, top_k)
        return primary

    @classmethod
    def _split_precision_layers(
        cls,
        unique: List[Dict[str, object]],
        top_k: int,
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """主列表保准；挤出的候选进 also_consider 防漏。"""
        if not unique:
            return [], []

        if cls._has_strong_symbol(unique):
            top_score = float(unique[0].get("score") or 0)
            floor = top_score * cls.SCORE_RATIO_FLOOR
            trimmed = [it for it in unique if float(it.get("score") or 0) >= floor]
            if not trimmed:
                trimmed = unique[:1]
            defs = [
                it
                for it in trimmed
                if str(it.get("match_source") or "") == "exact"
                and str(it.get("exact_tier") or "") in {"symbol", "symbol_weak"}
            ]
            if defs:
                trimmed = defs
            cap = min(max(1, top_k), cls.STRONG_SYMBOL_CAP)
            primary = trimmed[:cap]
        else:
            top_score = float(unique[0].get("score") or 0)
            floor = top_score * cls.RELATED_WEAK_SCORE_RATIO
            trimmed = [it for it in unique if float(it.get("score") or 0) >= floor]
            if not trimmed:
                trimmed = unique[:1]
            trimmed = cls._apply_dir_quota(trimmed, cls.RELATED_WEAK_DIR_QUOTA)
            cap = min(max(1, top_k), cls.RELATED_WEAK_CAP)
            primary = trimmed[:cap]

        primary_keys = {cls._file_key(it) for it in primary if cls._file_key(it)}
        also_floor = float(unique[0].get("score") or 0) * cls.RELATED_ALSO_SCORE_RATIO
        also: List[Dict[str, object]] = []
        for it in unique:
            fp = cls._file_key(it)
            if not fp or fp in primary_keys:
                continue
            if float(it.get("score") or 0) < also_floor:
                continue
            also.append(it)
            if len(also) >= cls.RELATED_ALSO_CONSIDER_CAP:
                break
        return primary, also

    @classmethod
    def fuse_related_layers(
        cls,
        *,
        exact_items: List[Dict[str, object]],
        symbol_docs: List[Dict[str, object]],
        top_k: int,
        extra_items: Optional[List[Dict[str, object]]] = None,
        keywords: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """精确定位分层：items 保准，also_consider 防漏；图谱默认不进主通道。"""
        path_keywords = cls._path_keywords_for_bonus(keywords)
        ranked: List[Dict[str, object]] = []
        for it in exact_items:
            raw = float(it.get("_score") or 0)
            tier = str(it.get("exact_tier") or ("symbol" if raw >= 2.8 else "path"))
            boost = cls._exact_boost(tier)
            fp = str(it.get("file_path") or "").replace("\\", "/")
            score = raw + boost
            score += cls._keyword_path_bonus(fp, path_keywords)
            ranked.append(
                {
                    "file_path": it.get("file_path"),
                    "symbol_kind": it.get("symbol_kind"),
                    "symbol_name": it.get("symbol_name"),
                    "start_line": it.get("start_line"),
                    "end_line": it.get("end_line"),
                    "score": score,
                    "_raw_score": raw,
                    "match_source": "exact",
                    "exact_tier": tier,
                }
            )
        for doc in symbol_docs:
            item = cls._normalize_vector_item(doc, "symbol_summary")
            fp = str(item.get("file_path") or "").replace("\\", "/")
            item["score"] = float(item.get("score") or 0) + cls._keyword_path_bonus(fp, path_keywords)
            ranked.append(item)
        for it in extra_items or []:
            row = dict(it)
            fp = str(row.get("file_path") or "").replace("\\", "/")
            if str(row.get("match_source") or "") == "codegraph":
                row["score"] = float(row.get("score") or 0) * 0.35
            row["score"] = float(row.get("score") or 0) + cls._keyword_path_bonus(fp, path_keywords)
            ranked.append(row)

        ranked.sort(key=cls._rank_key)
        best_by_file: Dict[str, Dict[str, object]] = {}
        for it in ranked:
            fp = str(it.get("file_path") or "")
            if not fp or RelatedKeywordExpander.is_noise_path(fp):
                continue
            prev = best_by_file.get(fp)
            if prev is None or cls._rank_key(it) < cls._rank_key(prev):
                best_by_file[fp] = it
        unique = sorted(best_by_file.values(), key=cls._rank_key)
        return cls._split_precision_layers(unique, top_k)

    @classmethod
    def fuse_related_items(
        cls,
        *,
        exact_items: List[Dict[str, object]],
        symbol_docs: List[Dict[str, object]],
        top_k: int,
        extra_items: Optional[List[Dict[str, object]]] = None,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, object]]:
        """精确定位：强符号 > 符号摘要/路径；图谱默认不进主通道。"""
        primary, _also = cls.fuse_related_layers(
            exact_items=exact_items,
            symbol_docs=symbol_docs,
            top_k=top_k,
            extra_items=extra_items,
            keywords=keywords,
        )
        return primary

    @classmethod
    def _path_keywords_for_bonus(cls, keywords: Optional[List[str]]) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for raw in keywords or []:
            parts = RelatedKeywordExpander.core_tokens(str(raw or "")) + [str(raw or "").strip()]
            for part in parts:
                k = part.strip().lower().replace("\\", "/")
                if not k or len(k) < 3 or k in seen:
                    continue
                seen.add(k)
                out.append(k)
        return out

    @staticmethod
    def _keyword_path_bonus(file_path: str, path_keywords: List[str]) -> float:
        """路径命中关键词加分：文件名整段匹配优先于路径子串。"""
        if not path_keywords or not file_path:
            return 0.0
        fp = file_path.replace("\\", "/").lower()
        stem = fp.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        segments = [s for s in fp.split("/") if s]
        best = 0.0
        for k in path_keywords:
            if not k:
                continue
            kl = k.lower().replace("-", "_")
            if stem == kl or stem.endswith(f"_{kl}") or stem.startswith(f"{kl}_"):
                best = max(best, 1.25)
            elif kl in segments:
                best = max(best, 1.05)
            elif kl in fp:
                best = max(best, 0.85)
            elif kl.rstrip("s") and any(
                seg.startswith(kl) or kl.startswith(seg.rstrip("s"))
                for seg in segments
                if len(seg) >= 4
            ):
                best = max(best, 0.95)
        return best

    @classmethod
    async def _search_codegraph_files(
        cls,
        repo_id: str,
        keywords: List[str],
        *,
        top_k: int,
    ) -> List[Dict[str, object]]:
        if not settings.code_graph_enabled:
            return []
        from .codegraph.gateway import CodeGraphGateway

        items: List[Dict[str, object]] = []
        try:
            with CodeGraphGateway.create_search() as q:
                for kw in keywords:
                    for method_name, hit_key in (
                        ("query_callers_of_symbol", "callers"),
                        ("query_callees_of_symbol", "callees"),
                    ):
                        method = getattr(q, method_name, None)
                        if method is None:
                            continue
                        res = await method(repo_id, kw, limit=top_k)
                        if not res.result:
                            continue
                        content = res.content or {}
                        for hit in content.get(hit_key) or []:
                            fp = str(hit.get("file_path") or "").replace("\\", "/")
                            if not fp:
                                continue
                            items.append(
                                {
                                    "file_path": fp,
                                    "symbol_kind": hit.get("kind"),
                                    "symbol_name": hit.get("name") or kw,
                                    "start_line": hit.get("start_line"),
                                    "end_line": hit.get("end_line"),
                                    "score": cls.CODEGRAPH_SCORE,
                                    "match_source": "codegraph",
                                    "exact_tier": None,
                                }
                            )
        except Exception as e:
            logging.warning("related CodeGraph 通道失败 repo_id=%s error=%s", repo_id, e)
        return items

    @staticmethod
    def related_channel_flags() -> Dict[str, bool]:
        """定位通道开关：符号摘要优先；关闭时用路径 exact（行块元数据）兜底；图谱需显式打开。"""
        symbol_on = bool(settings.code_analysis_symbol_summary_enabled)
        chunk_on = bool(settings.code_analysis_line_chunk_enabled)
        return {
            "symbol": symbol_on,
            "path_fallback": (not symbol_on) and chunk_on,
            "codegraph": bool(
                settings.code_graph_enabled
                and bool(getattr(settings, "code_analysis_related_include_graph", False))
            ),
        }

    @staticmethod
    def capability_flags() -> Dict[str, bool]:
        return {
            "chunk": bool(settings.code_analysis_line_chunk_enabled),
            "symbol": bool(settings.code_analysis_symbol_summary_enabled),
            "codegraph": bool(settings.code_graph_enabled),
            "mr_experience": bool(settings.mr_experience_enabled),
        }

    @staticmethod
    async def search_similar_code(
        repo_id: str,
        code_text: str,
        top_k: int = 10,
        *,
        nl_prep: Optional[NlQueryPrepResult] = None,
    ) -> Dict[str, object]:
        if not settings.code_analysis_line_chunk_enabled:
            raise ValueError(
                "行块能力已关闭：请设置 CODE_ANALYSIS_LINE_CHUNK_ENABLED=true"
            )
        query = (code_text or "").strip()
        if not query:
            raise ValueError("code_text 不能为空")

        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")

        prep = nl_prep or await NlQueryPrep.prepare(repo_id, query, rewrite="auto")
        query = prep.original_query or query
        embed_queries = prep.embed_queries
        fetch_k = max(top_k * SearchService.SIMILAR_FETCH_MULTIPLIER, SearchService.SIMILAR_MIN_FETCH)
        doc_batches: List[List[Dict[str, object]]] = []
        for q in embed_queries:
            docs = await CodeVectorSearchService.search_code_chunk_vectors(repo_id, [q], fetch_k)
            if docs:
                doc_batches.append(docs)
        merged_docs = SearchService._merge_similar_docs(doc_batches)
        unique = SearchService.fuse_similar_items(
            merged_docs,
            query_text=query,
            top_k=top_k,
        )
        index = await SearchIndexMeta.for_repo(repo_id)
        out: Dict[str, object] = {
            "repo_id": repo_id,
            "total": len(unique),
            "index": index,
            "items": unique,
        }
        meta = prep.meta()
        if meta is not None:
            out["nl_rewrite"] = {
                "english": meta.get("english"),
                "identifiers": meta.get("identifiers"),
            }
        return out

    @classmethod
    async def search_related_files(
        cls,
        repo_id: str,
        keywords: List[str],
        top_k: int = 10,
        *,
        nl_prep: Optional[NlQueryPrepResult] = None,
    ) -> Dict[str, object]:
        raw_keywords = [str(k).strip() for k in (keywords or []) if k and str(k).strip()]
        if not raw_keywords:
            raise ValueError("keywords 不能为空")

        channels = cls.related_channel_flags()
        if not any(channels.values()):
            raise ValueError(
                "related 能力全部关闭：请开启 CODE_ANALYSIS_SYMBOL_SUMMARY_ENABLED"
                "或 CODE_ANALYSIS_LINE_CHUNK_ENABLED"
                "（或 CODE_ANALYSIS_RELATED_INCLUDE_GRAPH=true 且 CODE_GRAPH_ENABLED）"
            )

        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")

        joined_kw = " ".join(raw_keywords)
        prep = nl_prep or await NlQueryPrep.prepare(
            repo_id,
            joined_kw,
            keywords=raw_keywords,
            rewrite="auto",
        )
        keywords = list(prep.keywords)

        fetch_k = max(top_k * 2, top_k)
        exact_items: List[Dict[str, object]] = []
        symbol_docs: List[Dict[str, object]] = []
        extra_items: List[Dict[str, object]] = []

        if channels["symbol"]:
            exact_items.extend(await ExactMatchService.match_symbols(repo_id, keywords, top_k=fetch_k))
            exact_items.extend(await ExactMatchService.match_paths(repo_id, keywords, top_k=fetch_k))
            symbol_docs = await CodeVectorSearchService.search_code_symbol_summary_vectors(
                repo_id, keywords, fetch_k
            )
        elif channels.get("path_fallback"):
            exact_items.extend(await ExactMatchService.match_paths(repo_id, keywords, top_k=fetch_k))
        if channels["codegraph"]:
            extra_items.extend(
                await cls._search_codegraph_files(repo_id, keywords, top_k=fetch_k)
            )

        primary, also_consider = cls.fuse_related_layers(
            exact_items=exact_items,
            symbol_docs=symbol_docs,
            top_k=top_k,
            extra_items=extra_items,
            keywords=keywords,
        )
        indexed_paths = await ExactMatchService.list_indexed_file_paths(repo_id)
        also_consider = DirSiblingExpander.expand(
            primary=primary,
            also=also_consider,
            candidate_paths=indexed_paths,
        )
        for it in primary:
            it.pop("_raw_score", None)
        for it in also_consider:
            it.pop("_raw_score", None)
        index = await SearchIndexMeta.for_repo(repo_id)
        out: Dict[str, object] = {
            "repo_id": repo_id,
            "keywords": keywords,
            "keywords_raw": raw_keywords,
            "channels": channels,
            "read_hint": cls.READ_HINT,
            "total": len(primary),
            "also_consider_total": len(also_consider),
            "index": index,
            "items": primary,
            "also_consider": also_consider,
        }
        meta = prep.meta()
        if meta is not None:
            out["nl_rewrite"] = {
                "english": meta.get("english"),
                "identifiers": meta.get("identifiers"),
            }
        return out

    @staticmethod
    async def search_chunks(
        repo_id: str,
        query: str,
        top_k: int = 10,
    ) -> Dict[str, object]:
        """仅行块向量检索（人工调试用）。"""
        if not settings.code_analysis_line_chunk_enabled:
            raise ValueError(
                "行块能力已关闭：请设置 CODE_ANALYSIS_LINE_CHUNK_ENABLED=true"
            )
        q = (query or "").strip()
        if not q:
            raise ValueError("query 不能为空")
        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            kind = getattr(repo, "kind", None) or RepoKind.CODE
            if kind != RepoKind.CODE:
                raise ValueError(f"search chunks 仅支持 kind=code，当前 kind={kind}")

        docs = await CodeVectorSearchService.search_code_chunk_vectors(repo_id, [q], top_k)
        index = await SearchIndexMeta.for_repo(repo_id)
        return {
            "repo_id": repo_id,
            "query": q,
            "total": len(docs),
            "index": index,
            "items": [
                {
                    "file_path": doc.get("file_path"),
                    "start_line": doc.get("start_line"),
                    "end_line": doc.get("end_line"),
                    "content_preview": str(doc.get("content") or "")[:200],
                    "score": doc.get("_score"),
                    "match_source": "line_chunk",
                }
                for doc in docs
            ],
        }

    @staticmethod
    async def search_symbols(
        repo_id: str,
        query: str,
        top_k: int = 10,
    ) -> Dict[str, object]:
        """仅符号摘要向量检索（人工调试用）。"""
        if not settings.code_analysis_symbol_summary_enabled:
            raise ValueError(
                "符号能力已关闭：请设置 CODE_ANALYSIS_SYMBOL_SUMMARY_ENABLED=true"
            )
        q = (query or "").strip()
        if not q:
            raise ValueError("query 不能为空")
        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            kind = getattr(repo, "kind", None) or RepoKind.CODE
            if kind != RepoKind.CODE:
                raise ValueError(f"search symbols 仅支持 kind=code，当前 kind={kind}")

        docs = await CodeVectorSearchService.search_code_symbol_summary_vectors(repo_id, [q], top_k)
        index = await SearchIndexMeta.for_repo(repo_id)
        return {
            "repo_id": repo_id,
            "query": q,
            "total": len(docs),
            "index": index,
            "items": [
            {
                "file_path": doc.get("file_path"),
                    "symbol_kind": doc.get("symbol_kind"),
                    "symbol_name": doc.get("symbol_name"),
                "start_line": doc.get("start_line"),
                "end_line": doc.get("end_line"),
                    "summary": doc.get("summary"),
                    "score": doc.get("_score"),
                    "match_source": "symbol_summary",
            }
            for doc in docs
            ],
        }

    @staticmethod
    async def search_patterns(
        repo_id: str,
        query: str,
        top_k: int = 10,
    ) -> Dict[str, object]:
        if not settings.mr_experience_enabled:
            raise ValueError("MR 经验能力已关闭：请设置 MR_EXPERIENCE_ENABLED=true")
        q = (query or "").strip()
        if not q:
            raise ValueError("query 不能为空")

        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            kind = getattr(repo, "kind", None) or RepoKind.CODE
            if kind != RepoKind.CODE:
                raise ValueError(f"search pattern 仅支持 kind=code，当前 kind={kind}")

        if not await PatternVectorService.space_exists(repo_id):
            raise ValueError("该仓库尚无经验数据，请先执行 experience analyze")

        items = await PatternVectorService.search(repo_id, q, top_k)
        min_score = float(settings.mr_experience_min_quality_score or 0.0)
        items = [it for it in items if float(it.get("quality_score") or 0.0) >= min_score]
        if settings.mr_experience_merge_by_scenario:
            items = PatternVectorService.merge_by_scenario(items)
        items = items[: max(1, top_k)]
        index = await SearchIndexMeta.for_repo(repo_id)
        return {
            "repo_id": repo_id,
            "query": q,
            "total": len(items),
            "index": index,
            "items": items,
        }
