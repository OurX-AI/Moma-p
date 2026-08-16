from __future__ import annotations
import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Set, Tuple


class ResolveResultPresenter:
    """把融合命中整理成 Agent 可读的 why / summary / TopN。"""

    AGENT_ITEM_LIMIT = 3
    ALSO_CONSIDER_CAP = 8
    READ_HINT = "优先读 items；改代码前扫 also_consider，防漏相关文件"

    _SOURCE_HINT = {
        "exact": "精确命中",
        "grep": "全文/标识符命中",
        "symbol_summary": "符号摘要相关",
        "line_chunk": "相似代码块",
        "codegraph": "图谱关系",
        "mr_experience": "历史经验",
        "api": "公开接口",
    }

    _TEST_PATH_MARKERS = (
        "/test/",
        "/tests/",
        "/testing/",
        "/__tests__/",
        "_test.",
        ".test.",
        "_spec.",
        ".spec.",
        "/fixtures/",
    )
    _VENDOR_PATH_MARKERS = (
        "/bundled/",
        "/vendor/",
        "/third_party/",
        "/third-party/",
        "/node_modules/",
        "/external/",
    )
    # 跨语言常见「入口/杂项」文件名启发式，不含具体项目名（禁止 nng/express 等评测特化）
    _UMBRELLA_NAMES = {
        "index.js",
        "index.ts",
        "index.tsx",
        "main.go",
        "main.c",
        "main.cpp",
        "main.py",
        "main.rs",
        "main.java",
        "common.h",
        "common.hpp",
        "common.py",
        "types.h",
        "types.ts",
        "util.go",
        "utils.go",
        "utils.py",
        "utils.js",
        "helpers.js",
        "helpers.py",
        "misc.py",
        "base.py",
    }
    _UMBRELLA_STEMS = {
        "index",
        "main",
        "common",
        "util",
        "utils",
        "helpers",
        "misc",
        "base",
        "types",
    }
    _FAMILY_SUFFIXES = ("-inl", "_inl", "-impl", "_impl", ".min", "-internal")
    _TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")

    @classmethod
    def why_for(cls, item: Dict[str, object]) -> str:
        channel = str(item.get("channel") or "")
        source = str(item.get("match_source") or "")
        hint = cls._SOURCE_HINT.get(source) or source or channel or "命中"
        if item.get("fallback"):
            hint = f"弱相关兜底·{hint}"
        fp = str(item.get("file_path") or "").strip()
        symbol = str(item.get("symbol_name") or "").strip()
        title = str(item.get("title") or item.get("pattern_title") or "").strip()
        relation = str(item.get("graph_relation") or "").strip()
        target = str(item.get("graph_target") or "").strip()

        if relation and fp:
            focus = f"{relation} → {fp}"
            if target:
                focus = f"{relation}({target}) → {fp}"
        elif symbol and fp:
            focus = f"{fp}#{symbol}"
        elif fp:
            focus = fp
        elif title:
            focus = title
        elif symbol:
            focus = symbol
        else:
            focus = "未定位到路径"
        return f"[{channel or source}] {hint}：{focus}"

    @classmethod
    def annotate(cls, items: List[Dict[str, object]]) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for it in items:
            row = dict(it)
            row["why"] = cls.why_for(row)
            out.append(row)
        return out

    @classmethod
    def _item_key(cls, it: Dict[str, object]) -> str:
        fp = str(it.get("file_path") or "")
        if fp:
            return f"file:{fp}"
        title = str(it.get("title") or "")
        if title:
            return f"title:{title}"
        return str(it.get("symbol_name") or id(it))

    @classmethod
    def _normalize_path(cls, file_path: str) -> str:
        return str(file_path or "").replace("\\", "/").strip()

    @classmethod
    def query_tokens(cls, query: str) -> List[str]:
        """从查询抽取可用于路径/符号对齐的词元。"""
        text = str(query or "").strip()
        if not text:
            return []
        out: List[str] = []
        seen: Set[str] = set()
        for raw in cls._TOKEN_RE.findall(text):
            tok = raw.strip().lower()
            if len(tok) < 2:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
            # res.json / app.use 类：保留点号前后已由 regex 切开
            parts = re.findall(r"[a-z0-9]+", tok)
            if len(parts) >= 2:
                for p in parts:
                    if len(p) >= 2 and p not in seen:
                        seen.add(p)
                        out.append(p)
        return out

    @classmethod
    def _path_depth(cls, file_path: str) -> int:
        fp = cls._normalize_path(file_path)
        if not fp:
            return 0
        return len([p for p in PurePosixPath(fp).parts if p not in (".", "")])

    @classmethod
    def path_noise_penalty(cls, file_path: str) -> int:
        """路径噪声惩罚：越大越不该进 items 主列表。"""
        fp = cls._normalize_path(file_path).lower()
        if not fp:
            return 0
        pen = 0
        if any(m in fp for m in cls._TEST_PATH_MARKERS):
            pen += 40
        name = PurePosixPath(fp).name
        if name.endswith(("_test.go", "_test.py", "_test.c", "_test.cpp", "_test.js")):
            pen += 40
        if name.startswith("test_") and name.endswith((".py", ".go", ".js")):
            pen += 30
        if any(m in fp for m in cls._VENDOR_PATH_MARKERS):
            pen += 35
        # 伞文件只惩罚浅路径入口/杂项；深路径 base.py 等常是真实基类实现
        depth = cls._path_depth(fp)
        if name in cls._UMBRELLA_NAMES:
            if depth <= 2:
                pen += 12
            elif depth <= 3:
                pen += 4
        stem = PurePosixPath(fp).stem.lower()
        if stem in cls._UMBRELLA_STEMS:
            if depth <= 2:
                pen += 6
            elif depth <= 3:
                pen += 2
        return pen

    @classmethod
    def path_family_key(cls, file_path: str) -> str:
        """同实现族（如 logger.h / logger-inl.h）归并键。"""
        fp = cls._normalize_path(file_path)
        if not fp:
            return ""
        path = PurePosixPath(fp)
        stem = path.stem.lower()
        for suf in cls._FAMILY_SUFFIXES:
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
                break
        parent = str(path.parent).replace("\\", "/")
        return f"{parent}/{stem}"

    @classmethod
    def _ident_parts(cls, text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", (text or "").lower())

    @classmethod
    def _camel_parts(cls, text: str) -> List[str]:
        """保留驼峰边界后再小写，避免 ModelAdmin→modeladmin 丢片段。"""
        raw = str(text or "").strip()
        if not raw:
            return []
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", raw)
        if parts:
            return [p.lower() for p in parts if p]
        return cls._ident_parts(raw)

    @classmethod
    def _token_hits_text(
        cls,
        tok: str,
        text: str,
        *,
        allow_prefix: bool = True,
        max_prefix_extra: int = 10,
    ) -> bool:
        """词元命中标识符片段；避免短词误伤（如 res⊂express）。"""
        if not tok or not text:
            return False
        parts = cls._ident_parts(text)
        if tok in parts:
            return True
        if not allow_prefix:
            return False
        # 前缀对齐：app→application（extra=8）；文件名允许较宽，符号侧宜更紧
        if len(tok) >= 3:
            for p in parts:
                if p.startswith(tok) and len(p) - len(tok) <= max_prefix_extra:
                    return True
        return False

    @classmethod
    def _path_segment_hit(cls, tok: str, file_path: str) -> bool:
        """路径段精确命中；禁止 model⊂models 这类目录前缀蹭分。"""
        if not tok or not file_path:
            return False
        segs = [s for s in re.split(r"[/_.\\-]+", file_path.lower()) if s]
        return tok in segs

    @classmethod
    def query_relevance_parts(cls, it: Dict[str, object], query: str) -> Tuple[int, int]:
        """返回 (strong, weak)。strong 才允许抬档；weak 只参与同分排序。"""
        tokens = cls.query_tokens(query)
        if not tokens:
            if it.get("nl_token_hit") or it.get("nl_alias_hit"):
                return (0, 2)
            return (0, 0)
        fp = cls._normalize_path(str(it.get("file_path") or "")).lower()
        symbol_raw = str(it.get("symbol_name") or "").strip()
        symbol = symbol_raw.lower()
        symbol_parts = cls._camel_parts(symbol_raw)
        title = str(it.get("title") or it.get("pattern_title") or "").strip().lower()
        stem = PurePosixPath(fp).stem.lower() if fp else ""
        name = PurePosixPath(fp).name.lower() if fp else ""
        strong = 0
        weak = 0
        for tok in tokens:
            if symbol and tok == symbol:
                strong += 8
            elif symbol and tok in symbol_parts:
                # 驼峰整词命中：仅作弱信号，避免 Model⊂ModelAdmin 抬到 exact 同档
                weak += 3
            elif symbol and cls._token_hits_text(
                tok, symbol, allow_prefix=True, max_prefix_extra=4
            ):
                weak += 2
            if stem and tok == stem:
                strong += 7
            elif name and (tok == PurePosixPath(name).stem.lower() or tok == name):
                strong += 6
            # 文件名/stem 前缀放宽：保住 app→application；目录段仍禁止 model⊂models
            elif name and cls._token_hits_text(
                tok, name, allow_prefix=True, max_prefix_extra=10
            ):
                strong += 4
            elif stem and cls._token_hits_text(
                tok, stem, allow_prefix=True, max_prefix_extra=10
            ):
                strong += 4
            elif cls._path_segment_hit(tok, fp):
                weak += 2
            elif title and cls._token_hits_text(tok, title, allow_prefix=False):
                weak += 1
        if it.get("nl_token_hit") or it.get("nl_alias_hit"):
            weak += 3
        return (strong, weak)

    @classmethod
    def query_relevance(cls, it: Dict[str, object], query: str) -> int:
        """查询与路径/符号的对齐分；越高越应进入 items。"""
        strong, weak = cls.query_relevance_parts(it, query)
        return strong + weak

    @classmethod
    def _prefer_key(
        cls,
        it: Dict[str, object],
        *,
        query: str = "",
    ) -> Tuple[int, int, int, float]:
        source = str(it.get("match_source") or "")
        tier = str(it.get("exact_tier") or "")
        score = float(it.get("score") or it.get("quality_score") or it.get("similarity") or 0)
        if source == "exact" and tier == "symbol":
            band = 0
        elif source == "exact":
            band = 1
        elif it.get("nl_token_hit") or it.get("nl_alias_hit"):
            band = 1
        elif source == "grep" and score >= 2.5:
            band = 2
        elif source == "symbol_summary":
            band = 3
        elif source in {"codegraph", "graph"}:
            band = 4
        elif source == "grep":
            band = 5
        elif source == "line_chunk":
            band = 6
        else:
            band = 7
        penalty = cls.path_noise_penalty(str(it.get("file_path") or ""))
        strong, weak = cls.query_relevance_parts(it, query)
        relevance = strong + weak
        # 仅强对齐（符号全名 / 文件名 stem）可抬档；路径弱前缀不得抬到 exact 同档
        if strong >= 8:
            band = min(band, 0)
        elif strong >= 6:
            band = min(band, 1)
        elif strong >= 4:
            band = min(band, 2)
        if strong >= 6 and penalty > 0:
            penalty = max(0, penalty - 8)
        # 弱相关的浅路径伞文件额外惩罚
        fp = cls._normalize_path(str(it.get("file_path") or ""))
        name = PurePosixPath(fp).name.lower()
        if (
            name in cls._UMBRELLA_NAMES
            and cls._path_depth(fp) <= 3
            and strong < 4
        ):
            penalty += 10
        return (band, penalty, -relevance, -score)

    @classmethod
    def _ordered_items(
        cls,
        items: Sequence[Dict[str, object]],
        *,
        query: str = "",
    ) -> List[Dict[str, object]]:
        return sorted(items, key=lambda it: cls._prefer_key(it, query=query))

    @classmethod
    def _pick_primary(
        cls,
        ordered: Sequence[Dict[str, object]],
        *,
        limit: int,
        skip_keys: Optional[Set[str]] = None,
    ) -> List[Dict[str, object]]:
        """按序挑主列表，同实现族只留一条。"""
        primary: List[Dict[str, object]] = []
        seen: Set[str] = set(skip_keys or ())
        seen_families: Set[str] = set()
        for it in ordered:
            k = cls._item_key(it)
            if not k or k in seen:
                continue
            fam = cls.path_family_key(str(it.get("file_path") or ""))
            if fam and fam in seen_families:
                continue
            seen.add(k)
            if fam:
                seen_families.add(fam)
            primary.append(it)
            if len(primary) >= limit:
                break
        return primary

    @classmethod
    def agent_items(
        cls,
        items: List[Dict[str, object]],
        *,
        query: str = "",
    ) -> List[Dict[str, object]]:
        """对外最多 Top3；精确命中优先；若有兜底条，强制占 1 席。"""
        limit = cls.AGENT_ITEM_LIMIT
        if not items:
            return []

        ordered = cls._ordered_items(items, query=query)
        fallback = next((it for it in ordered if it.get("fallback")), None)
        if fallback is None:
            return cls._pick_primary(ordered, limit=limit)

        primary = cls._pick_primary(
            [it for it in ordered if not it.get("fallback")],
            limit=max(0, limit - 1),
            skip_keys={cls._item_key(fallback)},
        )
        return primary + [fallback]

    @classmethod
    def also_consider_items(
        cls,
        items: List[Dict[str, object]],
        primary: List[Dict[str, object]],
        *,
        query: str = "",
    ) -> List[Dict[str, object]]:
        """主列表之外的融合候选，供 Agent 防漏扫路径（默认不带 snippet）。"""
        if not items:
            return []
        primary_keys = {cls._item_key(it) for it in primary}
        ordered = cls._ordered_items(items, query=query)
        out: List[Dict[str, object]] = []
        seen: set[str] = set()
        for it in ordered:
            k = cls._item_key(it)
            if not k or k in primary_keys or k in seen:
                continue
            seen.add(k)
            out.append(it)
            if len(out) >= cls.ALSO_CONSIDER_CAP:
                break
        return out

    @classmethod
    def split_for_agent(
        cls,
        items: List[Dict[str, object]],
        *,
        query: str = "",
    ) -> tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        primary = cls.agent_items(items, query=query)
        also = cls.also_consider_items(items, primary, query=query)
        return primary, also

    @classmethod
    def summary(
        cls,
        *,
        intent: str,
        items: List[Dict[str, object]],
        fused_total: int,
        fallback_used: Optional[str],
        also_count: int = 0,
    ) -> str:
        if not items:
            base = f"{intent} 未命中可用结果"
            if fallback_used:
                return f"{base}（已尝试 {fallback_used} 兜底仍空）"
            return base
        top = items[0]
        top_focus = str(top.get("file_path") or top.get("title") or top.get("symbol_name") or "?")
        symbol = str(top.get("symbol_name") or "").strip()
        if symbol and top.get("file_path"):
            top_focus = f"{top.get('file_path')}#{symbol}"
        parts = [
            f"{intent} 推荐 {len(items)} 条（融合池 {fused_total}）",
            f"优先看 {top_focus}",
        ]
        if also_count > 0:
            parts.append(f"另有 {also_count} 条 also_consider 防漏")
        if fallback_used:
            parts.append(f"已附带 {fallback_used} 兜底")
        return "；".join(parts)
