from __future__ import annotations
import logging
from typing import Dict, List, Optional, Tuple
from app.config.settings import settings
from ..nl2code_enhance.weakness import NlRetrievalWeakness
from .intent import ResolvePlan, SearchIntent
from ..search_service import SearchService


class ResolveWeakFallback:
    """related 结果偏弱时，自动附带 1 条 pattern 或 graph 兜底。"""

    WEAK_SCORE_THRESHOLD = NlRetrievalWeakness.WEAK_SCORE_THRESHOLD

    @classmethod
    def is_weak(cls, intent: SearchIntent, items: List[Dict[str, object]]) -> bool:
        return NlRetrievalWeakness.needs_channel_fallback(intent, items)

    @classmethod
    async def try_one(
        cls,
        repo_id: str,
        plan: ResolvePlan,
        related_items: List[Dict[str, object]],
        *,
        run_channel,
    ) -> Tuple[Optional[Dict[str, object]], Optional[str], Optional[Dict[str, object]], Optional[str]]:
        """
        Returns:
            (fallback_item, channel_name, section_payload, error)
        """
        pattern_item, pattern_section, pattern_err = await cls._try_pattern(
            repo_id, plan, run_channel=run_channel
        )
        if pattern_item is not None:
            return pattern_item, "pattern", pattern_section, None

        graph_item, graph_section, graph_err = await cls._try_graph(
            repo_id, plan, related_items, run_channel=run_channel
        )
        if graph_item is not None:
            return graph_item, "graph", graph_section, None

        err = pattern_err or graph_err
        return None, None, None, err

    @classmethod
    async def _try_pattern(
        cls,
        repo_id: str,
        plan: ResolvePlan,
        *,
        run_channel,
    ) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]], Optional[str]]:
        if not settings.mr_experience_enabled:
            return None, None, "MR 经验能力已关闭"
        if "pattern" in plan.channels:
            return None, None, None
        try:
            section = await run_channel(repo_id, "pattern", plan, top_k=3)
        except Exception as exc:
            logging.warning("resolve pattern 兜底失败 repo_id=%s error=%s", repo_id, exc)
            return None, {"total": 0, "items": [], "error": str(exc)}, str(exc)
        items = list(section.get("items") or [])
        pick = cls._prefer_file_hit(items)
        if pick is None:
            return None, section, None
        row = dict(pick)
        row["fallback"] = True
        row.setdefault("channel", "pattern")
        row.setdefault("match_source", "mr_experience")
        return row, section, None

    @classmethod
    async def _try_graph(
        cls,
        repo_id: str,
        plan: ResolvePlan,
        related_items: List[Dict[str, object]],
        *,
        run_channel,
    ) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]], Optional[str]]:
        if not settings.code_graph_enabled:
            return None, None, "CodeGraph 能力已关闭"
        if "graph" in plan.channels:
            return None, None, None
        graph_file = cls._pick_graph_file(related_items)
        if not graph_file:
            return None, None, "无可用于图谱兜底的文件"
        graph_plan = ResolvePlan(
            intent=SearchIntent.GRAPH,
            channels=["graph"],
            keywords=plan.keywords,
            code_text=plan.code_text,
            graph_file=graph_file,
            graph_symbol=None,
            graph_mode="dependents",
            reason="related 弱相关，图谱 dependents 兜底",
            fallback_from="related",
        )
        try:
            section = await run_channel(repo_id, "graph", graph_plan, top_k=3)
        except Exception as exc:
            logging.warning("resolve graph 兜底失败 repo_id=%s error=%s", repo_id, exc)
            return None, {"total": 0, "items": [], "error": str(exc)}, str(exc)
        items = list(section.get("items") or [])
        if not items:
            return None, section, None
        row = dict(items[0])
        row["fallback"] = True
        row.setdefault("channel", "graph")
        row.setdefault("match_source", "codegraph")
        row.setdefault("score", SearchService.CODEGRAPH_SCORE)
        return row, section, None

    @classmethod
    def _prefer_file_hit(cls, items: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
        for it in items:
            if it.get("file_path") and it.get("from_pattern_files"):
                return it
        for it in items:
            if it.get("file_path"):
                return it
        for it in items:
            if it.get("title"):
                return it
        return None

    @classmethod
    def _pick_graph_file(cls, related_items: List[Dict[str, object]]) -> Optional[str]:
        for it in related_items:
            fp = str(it.get("file_path") or "").strip().replace("\\", "/")
            if fp and ("/" in fp or "." in fp.rsplit("/", 1)[-1]):
                return fp
        return None
