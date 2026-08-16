from __future__ import annotations
from typing import Any
from .models import SkillMeta


def _as_num(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def _fmt_compact_count(value: float) -> str:
    n = int(value)
    if n >= 10_000:
        text = f"{n / 10_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}万"
    if n >= 1000:
        text = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    return str(n)


def download_count(meta: SkillMeta) -> float:
    extra = meta.extra or {}
    if meta.source == "skills-sh":
        return _as_num(extra.get("installs"))
    if meta.source == "clawhub":
        return _as_num(extra.get("downloads"))
    if meta.source == "lobehub":
        return _as_num(extra.get("install_count"))
    return 0.0


def popularity_score(meta: SkillMeta, *, query: str = "") -> float:
    return download_count(meta)


def popularity_label(meta: SkillMeta) -> str | None:
    count = download_count(meta)
    if count > 0:
        return f"{_fmt_compact_count(count)} 下载"
    if meta.trust_level == "builtin":
        return "系统自带"
    if meta.trust_level == "trusted":
        return "可信源"
    return None


def sort_skill_metas(items: list[SkillMeta], *, query: str = "") -> list[SkillMeta]:
    return sorted(
        items,
        key=lambda meta: (-popularity_score(meta), meta.name.lower()),
    )
