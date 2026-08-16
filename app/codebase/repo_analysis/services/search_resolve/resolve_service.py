from __future__ import annotations
import asyncio
import logging
from dataclasses import replace
from typing import Dict, List, Optional, Set
from sqlalchemy import select
from app.config.settings import settings
from app.infrastructure.database import get_db_session
from ..codegraph.gateway import CodeGraphGateway
from ..codevector.exact_match import ExactMatchService
from ..nl2code_enhance import (
    NlQueryPrep,
    NlQueryPrepResult,
    NlQueryRewriter,
    NlToCodeEnhancement,
    RelatedKeywordExpander,
)
from ..nl2code_enhance.keyword_expander import LexiconExpandable
from ..nl2code_enhance.weakness import NlRetrievalWeakness
from ..dir_sibling_expander import DirSiblingExpander
from ..search_index_meta import SearchIndexMeta
from .intent import ResolvePlan, SearchIntentRouter
from .readiness import ResolveReadiness
from .result_presenter import ResolveResultPresenter
from .weak_fallback import ResolveWeakFallback
from ..search_service import SearchService
from ....repo_mgmt.models.git_repo_mgmt import GitRepository, RepoKind


class SearchResolveService:
    """统一检索编排：规则路由 + 多通道并行 + 带来源融合结果。"""

    CHANNEL_PRIORITY = {
        "exact": 0,
        "symbol_summary": 1,
        "grep": 2,
        "line_chunk": 3,
        "mr_experience": 4,
        "api": 5,
        "codegraph": 6,
        "graph": 6,
    }

    @classmethod
    async def resolve(
        cls,
        repo_id: str,
        query: str,
        *,
        top_k: int = 10,
        intent: Optional[str] = None,
        require_searchable: bool = True,
    ) -> Dict[str, object]:
        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo:
                raise ValueError("仓库不存在")
            kind = getattr(repo, "kind", None) or RepoKind.CODE

        if require_searchable:
            await ResolveReadiness.ensure_searchable(repo_id)

        plan = SearchIntentRouter.plan(query, repo_kind=kind, intent_override=intent)
        base_keywords = list(plan.keywords or ([query] if query else []))
        nl_rewrite_meta: Optional[Dict[str, object]] = None
        nl_prep: Optional[NlQueryPrepResult] = None
        channel_errors: Dict[str, str] = {}
        if NlToCodeEnhancement.is_enabled():
            try:
                nl_prep = await cls._await_channel(
                    "nl_prep",
                    NlQueryPrep.prepare(
                        repo_id,
                        query,
                        keywords=base_keywords,
                        rewrite="auto",
                    ),
                )
                if nl_prep.rewrite and nl_prep.rewrite.seeds():
                    nl_rewrite_meta = nl_prep.meta()
                # 保留原始 query 作为 code_text，改写只进 keywords / embed 种子
                plan = replace(
                    plan,
                    keywords=list(nl_prep.keywords),
                    code_text=query,
                )
            except Exception as exc:
                channel_errors["nl_prep"] = str(exc)
                logging.warning(
                    "resolve NL 预备失败，降级为无 NL 增强 repo_id=%s error=%s",
                    repo_id,
                    exc,
                )
                nl_prep = None

        sections: Dict[str, object] = {}
        fused_items: List[Dict[str, object]] = []
        channels_used: List[str] = []

        tasks = []
        for channel in plan.channels:
            tasks.append(
                cls._run_channel_safe(repo_id, channel, plan, top_k=top_k, nl_prep=nl_prep)
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for channel, result in zip(plan.channels, results):
            if isinstance(result, Exception):
                channel_errors[channel] = str(result)
                logging.warning(
                    "resolve 通道失败 repo_id=%s channel=%s error=%s",
                    repo_id,
                    channel,
                    result,
                )
                sections[channel] = {"total": 0, "items": [], "error": str(result)}
                continue
            sections[channel] = result
            channels_used.append(channel)
            for item in result.get("items") or []:
                fused = dict(item)
                fused.setdefault("channel", channel)
                if channel == "pattern":
                    fused.setdefault("match_source", "mr_experience")
                elif channel == "api":
                    fused.setdefault("match_source", "api")
                elif channel == "graph":
                    fused.setdefault("match_source", "codegraph")
                elif channel == "grep":
                    fused.setdefault("match_source", "grep")
                fused_items.append(fused)

        fused_items = cls._fuse_items(fused_items, top_k=max(top_k * 2, top_k))
        try:
            lexicon = (nl_prep.lexicon if nl_prep else None) or await NlToCodeEnhancement.lexicon_for_repo(
                repo_id
            )
        except Exception as exc:
            channel_errors["lexicon"] = str(exc)
            logging.warning("resolve lexicon 失败 repo_id=%s error=%s", repo_id, exc)
            lexicon = nl_prep.lexicon if nl_prep else None
        if NlToCodeEnhancement.is_enabled():
            cls._mark_nl_token_hits(fused_items, plan.keywords or [query], lexicon=lexicon)

        if (
            NlToCodeEnhancement.is_enabled()
            and nl_rewrite_meta is None
            and NlQueryRewriter.should_rewrite_on_weak(query)
            and cls._is_nl_retrieval_weak(fused_items)
        ):
            try:
                rewrite_prep = await cls._await_channel(
                    "nl_rewrite",
                    NlQueryPrep.prepare(
                        repo_id,
                        query,
                        keywords=base_keywords,
                        rewrite="force",
                        rewrite_trigger="weak",
                    ),
                )
            except Exception as exc:
                channel_errors["nl_rewrite"] = str(exc)
                rewrite_prep = None
            if rewrite_prep and rewrite_prep.rewrite and rewrite_prep.rewrite.seeds():
                nl_rewrite_meta = rewrite_prep.meta()
                rewrite_plan = replace(
                    plan,
                    keywords=list(rewrite_prep.keywords),
                    code_text=query,
                )
                extra_channels = ["similar", "grep"]
                if "related" in plan.channels:
                    extra_channels.append("related")
                for channel in extra_channels:
                    try:
                        extra = await cls._run_channel_safe(
                            repo_id,
                            channel,
                            rewrite_plan,
                            top_k=top_k,
                            nl_prep=rewrite_prep,
                        )
                    except Exception as exc:
                        channel_errors[f"{channel}_nl_rewrite"] = str(exc)
                        continue
                    if channel not in channels_used:
                        channels_used.append(channel)
                    sections[f"{channel}_nl_rewrite"] = extra
                    for item in extra.get("items") or []:
                        fused = dict(item)
                        fused.setdefault("channel", channel)
                        fused["nl_rewrite"] = True
                        if channel == "grep":
                            fused.setdefault("match_source", "grep")
                        fused_items.append(fused)
                fused_items = cls._fuse_items(fused_items, top_k=max(top_k * 2, top_k))
                if NlToCodeEnhancement.is_enabled():
                    cls._mark_nl_token_hits(
                        fused_items,
                        rewrite_plan.keywords or [query],
                        lexicon=rewrite_prep.lexicon or lexicon,
                    )

        fallback_used: Optional[str] = None
        if ResolveWeakFallback.is_weak(plan.intent, fused_items):
            fb_item, fb_channel, fb_section, fb_err = await ResolveWeakFallback.try_one(
                repo_id,
                plan,
                fused_items,
                run_channel=cls._run_channel_safe,
            )
            if fb_channel and fb_section is not None and fb_channel not in sections:
                sections[fb_channel] = fb_section
            if fb_err and fb_channel:
                channel_errors[fb_channel] = fb_err
            if fb_item is not None and fb_channel:
                fused_items = cls._fuse_items(fused_items + [fb_item], top_k=top_k)
                fused_items = cls._ensure_fallback_marked(fused_items, fb_item)
                fallback_used = fb_channel
                if fb_channel not in channels_used:
                    channels_used.append(fb_channel)

        annotated = ResolveResultPresenter.annotate(fused_items)
        agent_items, also_consider = ResolveResultPresenter.split_for_agent(
            annotated,
            query=query,
        )
        if agent_items:
            indexed_paths = await ExactMatchService.list_indexed_file_paths(repo_id)
            also_consider = DirSiblingExpander.expand(
                primary=agent_items,
                also=also_consider,
                candidate_paths=indexed_paths,
            )
        index = await SearchIndexMeta.for_repo(repo_id)
        return {
            "repo_id": repo_id,
            "query": query,
            "intent": plan.intent.value,
            "intent_reason": plan.reason,
            "fallback_from": plan.fallback_from,
            "fallback_used": fallback_used,
            "nl_rewrite": nl_rewrite_meta,
            "channels_used": channels_used,
            "channel_errors": channel_errors or None,
            "plan": {
                "channels": plan.channels,
                "keywords": plan.keywords,
                "graph_file": plan.graph_file,
                "graph_symbol": plan.graph_symbol,
                "graph_mode": plan.graph_mode,
            },
            "summary": ResolveResultPresenter.summary(
                intent=plan.intent.value,
                items=agent_items,
                fused_total=len(annotated),
                fallback_used=fallback_used,
                also_count=len(also_consider),
            ),
            "read_hint": ResolveResultPresenter.READ_HINT,
            "total": len(agent_items),
            "also_consider_total": len(also_consider),
            "fused_total": len(annotated),
            "index": index,
            "items": agent_items,
            "also_consider": also_consider,
            "sections": sections,
        }

    @classmethod
    async def _await_channel(cls, name: str, awaitable):
        timeout_ms = int(settings.resolve_channel_timeout_ms or 0)
        if timeout_ms > 0:
            try:
                return await asyncio.wait_for(awaitable, timeout=timeout_ms / 1000.0)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"{name} 超时（{timeout_ms} ms）") from exc
        return await awaitable

    @classmethod
    async def _run_channel_safe(
        cls,
        repo_id: str,
        channel: str,
        plan: ResolvePlan,
        *,
        top_k: int,
        nl_prep: Optional[NlQueryPrepResult] = None,
    ) -> Dict[str, object]:
        return await cls._await_channel(
            channel,
            cls._run_channel(
                repo_id,
                channel,
                plan,
                top_k=top_k,
                nl_prep=nl_prep,
            ),
        )

    @classmethod
    def _is_nl_retrieval_weak(cls, items: List[Dict[str, object]]) -> bool:
        return NlRetrievalWeakness.needs_nl_rewrite(items)

    @classmethod
    def _mark_nl_token_hits(
        cls,
        items: List[Dict[str, object]],
        keywords: List[str],
        *,
        lexicon: Optional[LexiconExpandable] = None,
    ) -> None:
        for it in items:
            if RelatedKeywordExpander.path_matches_tokens(
                str(it.get("file_path") or ""),
                keywords,
                lexicon=lexicon,
            ):
                it["nl_token_hit"] = True

    @classmethod
    def _item_dedupe_key(cls, it: Dict[str, object]) -> str:
        fp = str(it.get("file_path") or "")
        if fp:
            return f"file:{fp}"
        title = str(it.get("title") or "")
        if title:
            return f"title:{title}"
        return str(it.get("symbol_name") or "")

    @classmethod
    def _ensure_fallback_marked(
        cls,
        items: List[Dict[str, object]],
        fallback_item: Dict[str, object],
    ) -> List[Dict[str, object]]:
        """融合后可能丢掉 fallback 标记；强制保留一条可展示的兜底命中。"""
        key = cls._item_dedupe_key(fallback_item)
        out = [dict(it) for it in items]
        if key:
            for it in out:
                if cls._item_dedupe_key(it) == key:
                    it["fallback"] = True
                    it.setdefault("channel", fallback_item.get("channel"))
                    it.setdefault("match_source", fallback_item.get("match_source"))
                    return out
        row = dict(fallback_item)
        row["fallback"] = True
        out.append(row)
        return out

    @classmethod
    async def _run_channel(
        cls,
        repo_id: str,
        channel: str,
        plan: ResolvePlan,
        *,
        top_k: int,
        nl_prep: Optional[NlQueryPrepResult] = None,
    ) -> Dict[str, object]:
        if channel == "similar":
            if not settings.code_analysis_line_chunk_enabled:
                raise ValueError("行块能力已关闭")
            result = await SearchService.search_similar_code(
                repo_id,
                plan.code_text,
                top_k=top_k,
                nl_prep=nl_prep,
            )
            return {"total": result.get("total"), "items": result.get("items") or []}

        if channel == "grep":
            return await cls._run_grep_channel(repo_id, plan, top_k=top_k, nl_prep=nl_prep)

        if channel == "related":
            keywords = plan.keywords or [plan.code_text]
            result = await SearchService.search_related_files(
                repo_id,
                keywords,
                top_k=top_k,
                nl_prep=nl_prep,
            )
            items = [dict(it) for it in (result.get("items") or [])]
            # related 次层并入融合池，供 resolve 的 also_consider 继承
            for it in result.get("also_consider") or []:
                row = dict(it)
                row["score"] = float(row.get("score") or 0) * 0.9
                row.setdefault("channel", "related")
                items.append(row)
            return {"total": len(items), "items": items}

        if channel == "pattern":
            if not settings.mr_experience_enabled:
                raise ValueError("MR 经验能力已关闭")
            result = await SearchService.search_patterns(repo_id, plan.code_text, top_k=top_k)
            items = []
            for it in result.get("items") or []:
                row = dict(it)
                row.setdefault("match_source", "mr_experience")
                row.setdefault("channel", "pattern")
                items.append(row)
                items.extend(cls._expand_pattern_file_hits(row))
            return {"total": len(items), "items": items}

        if channel == "api":
            from ....lib_analysis.services.search_service import LibSearchService

            result = await LibSearchService.search_apis(repo_id, plan.code_text, top_k=top_k)
            items = []
            for it in result.get("items") or []:
                row = dict(it)
                row["match_source"] = "api"
                row["symbol_name"] = row.get("api_name")
                row["symbol_kind"] = row.get("api_kind")
                items.append(row)
            return {"total": len(items), "items": items}

        if channel == "graph":
            return await cls._run_graph_channel(repo_id, plan, top_k=top_k)

        raise ValueError(f"未知通道: {channel}")

    @classmethod
    async def _run_grep_channel(
        cls,
        repo_id: str,
        plan: ResolvePlan,
        *,
        top_k: int,
        nl_prep: Optional[NlQueryPrepResult] = None,
    ) -> Dict[str, object]:
        if not settings.code_analysis_content_grep_enabled:
            raise ValueError("全文 grep 能力已关闭")
        from ..analysis_service import AnalysisService
        from ..content_grep import ContentGrepService

        async with get_db_session() as db:
            repo = await db.scalar(select(GitRepository).where(GitRepository.id == repo_id))
            if not repo or not repo.local_path:
                raise ValueError("仓库本地路径不可用")
            local_path = repo.local_path
        if nl_prep is not None:
            terms = RelatedKeywordExpander.prioritize_for_grep(list(nl_prep.keywords))
        else:
            terms = list(plan.keywords or [])
            if plan.code_text and plan.code_text not in terms:
                terms.append(plan.code_text)
            lexicon = await NlToCodeEnhancement.lexicon_for_repo(repo_id)
            terms = RelatedKeywordExpander.prioritize_for_grep(
                RelatedKeywordExpander.expand(terms, lexicon=lexicon)
            )
        items = ContentGrepService.search(
            local_path,
            terms,
            extensions=AnalysisService.CODE_EXTENSIONS,
            top_k=top_k,
            builtin_dir_names=AnalysisService.EXCLUDED_DIRS,
        )
        for it in items:
            it.setdefault("channel", "grep")
            it.setdefault("match_source", "grep")
        return {"total": len(items), "items": items}

    @classmethod
    async def _run_graph_channel(
        cls,
        repo_id: str,
        plan: ResolvePlan,
        *,
        top_k: int,
    ) -> Dict[str, object]:
        if not settings.code_graph_enabled:
            raise ValueError("CodeGraph 能力已关闭")
        items: List[Dict[str, object]] = []
        mode = plan.graph_mode or "dependents"
        with CodeGraphGateway.create_search() as q:
            if mode == "dependents" and plan.graph_file:
                res = await q.query_dependents_of_file(repo_id, plan.graph_file)
                if not res.result:
                    raise ValueError(res.message or "dependents 查询失败")
                for fp in (res.content or {}).get("dependents") or []:
                    items.append(
                        {
                            "file_path": str(fp).replace("\\", "/"),
                            "score": SearchService.CODEGRAPH_SCORE,
                            "match_source": "codegraph",
                            "graph_relation": "dependents",
                            "graph_target": plan.graph_file,
                        }
                    )
            elif mode == "dependencies" and plan.graph_file:
                res = await q.query_dependented_of_file(repo_id, plan.graph_file)
                if not res.result:
                    raise ValueError(res.message or "dependencies 查询失败")
                for fp in (res.content or {}).get("dependented") or []:
                    items.append(
                        {
                            "file_path": str(fp).replace("\\", "/"),
                            "score": SearchService.CODEGRAPH_SCORE,
                            "match_source": "codegraph",
                            "graph_relation": "dependencies",
                            "graph_target": plan.graph_file,
                        }
                    )
            elif mode == "callers" and plan.graph_symbol:
                res = await q.query_callers_of_symbol(repo_id, plan.graph_symbol, limit=top_k)
                if not res.result:
                    raise ValueError(res.message or "callers 查询失败")
                for hit in (res.content or {}).get("callers") or []:
                    items.append(
                        {
                            "file_path": str(hit.get("file_path") or "").replace("\\", "/"),
                            "symbol_name": hit.get("name"),
                            "symbol_kind": hit.get("kind"),
                            "start_line": hit.get("start_line"),
                            "end_line": hit.get("end_line"),
                            "score": SearchService.CODEGRAPH_SCORE,
                            "match_source": "codegraph",
                            "graph_relation": "callers",
                            "graph_target": plan.graph_symbol,
                        }
                    )
            elif mode == "callees" and plan.graph_symbol:
                res = await q.query_callees_of_symbol(repo_id, plan.graph_symbol, limit=top_k)
                if not res.result:
                    raise ValueError(res.message or "callees 查询失败")
                for hit in (res.content or {}).get("callees") or []:
                    items.append(
                        {
                            "file_path": str(hit.get("file_path") or "").replace("\\", "/"),
                            "symbol_name": hit.get("name"),
                            "symbol_kind": hit.get("kind"),
                            "start_line": hit.get("start_line"),
                            "end_line": hit.get("end_line"),
                            "score": SearchService.CODEGRAPH_SCORE,
                            "match_source": "codegraph",
                            "graph_relation": "callees",
                            "graph_target": plan.graph_symbol,
                        }
                    )
            else:
                raise ValueError("graph 通道缺少有效 file/symbol")
        return {"total": len(items), "items": items[: max(1, top_k)]}

    @classmethod
    def _expand_pattern_file_hits(cls, pattern_item: Dict[str, object]) -> List[Dict[str, object]]:
        """把经验条目里的 relevant_files/anchors 展开成可与 related 对齐的文件命中。"""
        files: List[str] = []
        for key in ("relevant_files", "anchors"):
            for raw in pattern_item.get(key) or []:
                fp = str(raw or "").strip().replace("\\", "/")
                if not fp or fp.startswith("title:"):
                    continue
                if "/" not in fp and "." not in fp.rsplit("/", 1)[-1]:
                    continue
                files.append(fp)
        if not files:
            return []
        base_score = float(
            pattern_item.get("similarity")
            or pattern_item.get("quality_score")
            or pattern_item.get("score")
            or 0.6
        )
        title = str(pattern_item.get("title") or "")
        out: List[Dict[str, object]] = []
        seen: Set[str] = set()
        for fp in files:
            if fp in seen:
                continue
            seen.add(fp)
            out.append(
                {
                    "file_path": fp,
                    "score": base_score,
                    "match_source": "mr_experience",
                    "channel": "pattern",
                    "pattern_title": title,
                    "from_pattern_files": True,
                }
            )
        return out

    @classmethod
    def _fuse_items(cls, items: List[Dict[str, object]], *, top_k: int) -> List[Dict[str, object]]:
        if not items:
            return []

        def _rank_key(it: Dict[str, object]) -> tuple:
            source = str(it.get("match_source") or "")
            channel = str(it.get("channel") or "")
            priority = cls.CHANNEL_PRIORITY.get(source, 9)
            if channel == "pattern" and source == "mr_experience":
                if it.get("from_pattern_files") or it.get("file_path"):
                    priority = 3
                else:
                    priority = 4
            score = float(it.get("score") or it.get("quality_score") or it.get("similarity") or 0)
            return (priority, -score, str(it.get("file_path") or ""), str(it.get("title") or ""))

        best_by_key: Dict[str, Dict[str, object]] = {}
        for it in items:
            fp = str(it.get("file_path") or "")
            title = str(it.get("title") or "")
            if fp:
                key = f"file:{fp}"
            elif title:
                key = f"title:{title}"
            else:
                key = str(it.get("symbol_name") or "")
            if not key:
                continue
            prev = best_by_key.get(key)
            if prev is None or _rank_key(it) < _rank_key(prev):
                best_by_key[key] = it
        unique = sorted(best_by_key.values(), key=_rank_key)
        return unique[: max(1, top_k)]
