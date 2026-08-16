"""NL 检索弱判定：改写触发与通道兜底共用分数门槛。"""
from __future__ import annotations
from typing import Dict, List


class NlRetrievalWeakness:
    """统一「弱召回」信号，避免 rewrite / ResolveWeakFallback 两套阈值漂移。"""

    WEAK_SCORE_THRESHOLD = 0.85
    _RELATED_INTENT = "related"

    @classmethod
    def top_score(cls, items: List[Dict[str, object]]) -> float:
        if not items:
            return 0.0
        top = items[0]
        return float(top.get("score") or top.get("quality_score") or top.get("similarity") or 0)

    @classmethod
    def is_score_weak(cls, items: List[Dict[str, object]]) -> bool:
        if not items:
            return True
        top = items[0]
        if str(top.get("match_source") or "") == "exact":
            return False
        return cls.top_score(items) < cls.WEAK_SCORE_THRESHOLD

    @classmethod
    def needs_nl_rewrite(cls, items: List[Dict[str, object]]) -> bool:
        """弱模式 LLM 改写：分数弱，且无 NL token 命中。"""
        if any(it.get("nl_token_hit") for it in (items or [])):
            return False
        return cls.is_score_weak(items)

    @classmethod
    def needs_channel_fallback(cls, intent: object, items: List[Dict[str, object]]) -> bool:
        """related 通道 pattern/graph 兜底。"""
        intent_val = getattr(intent, "value", intent)
        if str(intent_val).strip().lower() != cls._RELATED_INTENT:
            return False
        return cls.is_score_weak(items)
