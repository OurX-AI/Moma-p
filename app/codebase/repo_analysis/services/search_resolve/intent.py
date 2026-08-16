from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Set
from app.config.settings import settings


class SearchIntent(str, Enum):
    AUTO = "auto"
    SIMILAR = "similar"
    RELATED = "related"
    PATTERN = "pattern"
    API = "api"
    GRAPH = "graph"


INTENT_ALIASES = {
    "auto": SearchIntent.AUTO,
    "similar": SearchIntent.SIMILAR,
    "related": SearchIntent.RELATED,
    "locate": SearchIntent.RELATED,
    "pattern": SearchIntent.PATTERN,
    "experience": SearchIntent.PATTERN,
    "api": SearchIntent.API,
    "graph": SearchIntent.GRAPH,
}


@dataclass(frozen=True)
class ResolvePlan:
    intent: SearchIntent
    channels: List[str]
    keywords: List[str]
    code_text: str
    graph_file: Optional[str]
    graph_symbol: Optional[str]
    graph_mode: Optional[str]
    reason: str
    fallback_from: Optional[str] = None


class SearchIntentRouter:
    """规则路由：根据 query / kind / 可选 intent 覆盖生成 ResolvePlan。"""

    _CODE_HINT = re.compile(
        r"(?m)^\s*(async\s+def|def|class|func|public|private|protected)\b|"
        r"[{};]\s*$|"
        r"@\w+|"
        r"->\s*\w+",
    )
    _EXPERIENCE_HINT = re.compile(
        r"(怎么改|如何改|历史|经验|合入|复盘|MR|mr|pattern|playbook|改法|踩坑)",
        re.IGNORECASE,
    )
    _API_HINT = re.compile(r"(公开接口|接口摘要|API|api\b|lib\s*接口)", re.IGNORECASE)
    _GRAPH_HINT = re.compile(
        r"(谁依赖|依赖谁|依赖关系|影响面|callers?|callees?|dependents?|dependencies?|谁调用|调用了谁)",
        re.IGNORECASE,
    )
    _FILE_PATH = re.compile(
        r"(?P<path>(?:[A-Za-z0-9_\-./\\]+)\.(?:py|go|java|cpp|cc|cxx|c|h|hpp|hh|hxx|ts|tsx|js|jsx|mjs|cjs|rs))",
        re.IGNORECASE,
    )
    _SYMBOL = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,}|[a-z_][a-z0-9_]{2,})\b")
    _CN_PHRASE = re.compile(r"[\u4e00-\u9fff]{2,}")
    _CN_STOP = {
        "怎么改",
        "如何改",
        "历史",
        "经验",
        "合入",
        "复盘",
        "改法",
        "踩坑",
        "谁依赖",
        "依赖谁",
        "依赖关系",
        "影响面",
        "查找",
        "找到",
        "定位",
        "实现",
        "代码",
        "文件",
        "模块",
        "功能",
        "相关",
        "什么",
        "哪里",
        "哪个",
        "一个",
        "一下",
        "这个",
        "那个",
        "我们",
        "可以",
        "需要",
        "进行",
        "使用",
        "通过",
    }

    @classmethod
    def normalize_intent(cls, raw: Optional[str]) -> SearchIntent:
        text = (raw or "auto").strip().lower()
        if not text:
            return SearchIntent.AUTO
        intent = INTENT_ALIASES.get(text)
        if intent is None:
            allowed = ", ".join(sorted(INTENT_ALIASES.keys()))
            raise ValueError(f"不支持的 intent={raw!r}，可选: {allowed}")
        return intent

    @classmethod
    def locate_channels(cls) -> List[str]:
        """NL/定位默认并联：related（符号/图谱可用时）+ similar + grep（按开关裁剪）。

        符号摘要关闭时 related 不进定位并联（避免弱路径兜底冒充语义相关）；
        独立 `search related` 仍可走 path_fallback。
        """
        from ..search_service import SearchService

        channels: List[str] = []
        flags = SearchService.related_channel_flags()
        if flags.get("symbol") or flags.get("codegraph"):
            channels.append("related")
        if settings.code_analysis_line_chunk_enabled:
            channels.append("similar")
        if settings.code_analysis_content_grep_enabled:
            channels.append("grep")
        return channels

    @classmethod
    def plan(
        cls,
        query: str,
        *,
        repo_kind: str,
        intent_override: Optional[str] = None,
    ) -> ResolvePlan:
        q = (query or "").strip()
        if not q:
            raise ValueError("query 不能为空")
        kind = (repo_kind or "code").strip().lower()
        intent = cls.normalize_intent(intent_override)
        if intent == SearchIntent.AUTO:
            intent = cls._detect_intent(q, kind)

        if kind == "lib" and intent != SearchIntent.API:
            raise ValueError(f"kind=lib 仅支持 intent=api/auto，当前 intent={intent.value}")
        if kind == "code" and intent == SearchIntent.API:
            raise ValueError("kind=code 不支持 intent=api，请使用 kind=lib 或改用 related/pattern")

        if kind == "lib":
            return ResolvePlan(
                intent=SearchIntent.API,
                channels=["api"],
                keywords=cls._keywords_from_query(q),
                code_text=q,
                graph_file=None,
                graph_symbol=None,
                graph_mode=None,
                reason="lib 仓默认走 api",
            )

        if intent == SearchIntent.SIMILAR:
            return ResolvePlan(
                intent=intent,
                channels=["similar"],
                keywords=cls._keywords_from_query(q),
                code_text=q,
                graph_file=None,
                graph_symbol=None,
                graph_mode=None,
                reason="代码片段相似检索",
            )

        if intent == SearchIntent.PATTERN:
            from ..search_service import SearchService

            pattern_channels = ["pattern"]
            flags = SearchService.related_channel_flags()
            if flags.get("symbol") or flags.get("codegraph"):
                pattern_channels.append("related")
            return ResolvePlan(
                intent=intent,
                channels=pattern_channels,
                keywords=cls._keywords_from_query(q),
                code_text=q,
                graph_file=None,
                graph_symbol=None,
                graph_mode=None,
                reason="历史经验为主，related 辅助定位（按开关）",
            )

        if intent == SearchIntent.GRAPH:
            graph_file, graph_symbol, graph_mode = cls._extract_graph_target(q)
            if not graph_file and not graph_symbol:
                return ResolvePlan(
                    intent=SearchIntent.RELATED,
                    channels=cls.locate_channels(),
                    keywords=cls._keywords_from_query(q),
                    code_text=q,
                    graph_file=None,
                    graph_symbol=None,
                    graph_mode=None,
                    reason="graph 未能抽出文件/符号，降级 NL 定位并联",
                    fallback_from="graph",
                )
            return ResolvePlan(
                intent=intent,
                channels=["graph"],
                keywords=cls._keywords_from_query(q),
                code_text=q,
                graph_file=graph_file,
                graph_symbol=graph_symbol,
                graph_mode=graph_mode,
                reason="图谱依赖/调用关系",
            )

        locate = cls.locate_channels()
        return ResolvePlan(
            intent=SearchIntent.RELATED,
            channels=locate,
            keywords=cls._keywords_from_query(q),
            code_text=q,
            graph_file=None,
            graph_symbol=None,
            graph_mode=None,
            reason="NL 定位：可用通道并联（related/similar/grep 按开关）",
        )

    @classmethod
    def _detect_intent(cls, query: str, kind: str) -> SearchIntent:
        if kind == "lib":
            return SearchIntent.API
        if cls._GRAPH_HINT.search(query):
            return SearchIntent.GRAPH
        if cls._EXPERIENCE_HINT.search(query):
            return SearchIntent.PATTERN
        if cls._API_HINT.search(query) and kind == "lib":
            return SearchIntent.API
        if cls._looks_like_code(query):
            return SearchIntent.SIMILAR
        return SearchIntent.RELATED

    @classmethod
    def _looks_like_code(cls, query: str) -> bool:
        lines = [ln for ln in query.splitlines() if ln.strip()]
        if len(lines) >= 2 and cls._CODE_HINT.search(query):
            return True
        if len(query) >= 40 and cls._CODE_HINT.search(query):
            return True
        return False

    @classmethod
    def _keywords_from_query(cls, query: str) -> List[str]:
        if "," in query or "，" in query:
            parts = re.split(r"[,，]", query)
            return [p.strip() for p in parts if p.strip()]
        tokens = cls._SYMBOL.findall(query)
        cn_phrases = cls._CN_PHRASE.findall(query)
        stop: Set[str] = {
            "the",
            "and",
            "for",
            "with",
            "from",
            "this",
            "that",
            "how",
            "what",
            "when",
            "where",
            "谁依赖",
            "依赖谁",
            "怎么改",
            "如何改",
        }
        out: List[str] = []
        seen: Set[str] = set()

        def _add(token: str) -> None:
            t = (token or "").strip()
            if not t or t.lower() in stop or t in cls._CN_STOP or t in seen:
                return
            if len(t) < 2:
                return
            seen.add(t)
            out.append(t)

        for t in tokens:
            _add(t)
            if len(out) >= 8:
                break
        if len(out) < 8:
            for phrase in cn_phrases:
                _add(phrase)
                if len(out) >= 8:
                    break
        if out:
            compact = " ".join(query.split())
            if compact and compact not in out and compact not in seen:
                out.insert(0, compact[:120])
            return out[:8]
        compact = " ".join(query.split())
        return [compact[:120]] if compact else []

    @classmethod
    def _extract_graph_target(cls, query: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        file_match = cls._FILE_PATH.search(query)
        graph_file = file_match.group("path").replace("\\", "/") if file_match else None
        mode = "dependents"
        lower = query.lower()
        if "dependencies" in lower or "依赖谁" in query or "callees" in lower or "调用了谁" in query:
            mode = "dependencies" if graph_file else "callees"
        elif "dependents" in lower or "谁依赖" in query or "影响" in query:
            mode = "dependents" if graph_file else "callers"
        elif "callers" in lower or "谁调用" in query:
            mode = "callers"
        elif "callees" in lower:
            mode = "callees"

        graph_symbol = None
        if mode in {"callers", "callees"} or not graph_file:
            symbols = cls._SYMBOL.findall(query)
            for name in symbols:
                if name.lower() in {"callers", "callees", "dependents", "dependencies"}:
                    continue
                if graph_file and name in graph_file:
                    continue
                graph_symbol = name
                break
        if graph_file and mode in {"dependents", "dependencies"}:
            return graph_file, None, mode
        if graph_symbol and mode in {"callers", "callees"}:
            return None, graph_symbol, mode
        if graph_file:
            return graph_file, None, "dependents"
        if graph_symbol:
            return None, graph_symbol, "callers"
        return None, None, None
