"""NL→Code 查询 LLM 改写：通用提示，输出英文描述与标识符候选。"""
from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional
from app.config.settings import settings
from app.infrastructure.llms import llm_factory
from .gate import NlToCodeEnhancement
from .query_builder import NlCodeQueryBuilder


@dataclass
class NlRewriteResult:
    english_query: str = ""
    identifiers: List[str] = field(default_factory=list)

    def seeds(self) -> List[str]:
        out: List[str] = []
        if self.english_query.strip():
            out.append(self.english_query.strip())
        for ident in self.identifiers:
            t = str(ident or "").strip()
            if t:
                out.append(t)
        return out


class NlQueryRewriter:
    """把自然语言改写成英文描述 + 可能的代码标识符候选。"""

    SYSTEM = (
        "You rewrite code-search queries for retrieval. "
        "Output JSON only. Do not invent product-specific names you are unsure of; "
        "prefer generic English terms and plausible identifier shapes."
    )
    USER_TMPL = (
        "Rewrite the user query for code search.\n"
        "Return JSON object with keys:\n"
        '  "english": short English description of the functionality to find,\n'
        '  "identifiers": array of up to 8 plausible code identifiers '
        "(snake_case or CamelCase) that might appear in such code.\n"
        "Query: {query}"
    )
    _JSON_OBJ = re.compile(r"\{[\s\S]*\}")

    @classmethod
    def is_enabled(cls) -> bool:
        return NlToCodeEnhancement.is_enabled() and bool(settings.code_analysis_nl_rewrite_enabled)

    @classmethod
    def mode(cls) -> str:
        raw = str(settings.code_analysis_nl_rewrite_mode or "weak").strip().lower()
        return raw if raw in {"always", "weak"} else "weak"

    @classmethod
    def should_rewrite_upfront(cls, query: str) -> bool:
        if not cls.is_enabled():
            return False
        if cls.mode() != "always":
            return False
        return NlCodeQueryBuilder.looks_like_nl(query)

    @classmethod
    def should_rewrite_on_weak(cls, query: str) -> bool:
        if not cls.is_enabled():
            return False
        if cls.mode() != "weak":
            return False
        return NlCodeQueryBuilder.looks_like_nl(query)

    @classmethod
    def parse_response(cls, text: str) -> NlRewriteResult:
        raw = (text or "").strip()
        if not raw:
            return NlRewriteResult()
        m = cls._JSON_OBJ.search(raw)
        blob = m.group(0) if m else raw
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return NlRewriteResult(english_query=raw[:240])
        if not isinstance(data, dict):
            return NlRewriteResult()
        english = str(data.get("english") or data.get("query") or "").strip()
        idents_raw = data.get("identifiers") or data.get("idents") or []
        idents: List[str] = []
        if isinstance(idents_raw, str):
            idents_raw = [idents_raw]
        if isinstance(idents_raw, list):
            for x in idents_raw[:8]:
                t = str(x or "").strip()
                if t and t.isascii() and len(t) >= 3:
                    idents.append(t)
        return NlRewriteResult(english_query=english[:320], identifiers=idents)

    @classmethod
    async def rewrite(cls, query: str) -> Optional[NlRewriteResult]:
        q = (query or "").strip()
        if not q:
            return None
        try:
            llm = llm_factory.create_model()
            stream, _usage = await llm.chat_stream(
                system_prompt=cls.SYSTEM,
                user_prompt=cls.USER_TMPL.format(query=q),
                user_question=q,
            )
            chunks: List[str] = []
            async for piece in stream:
                chunks.append(piece)
            text = "".join(chunks).strip()
            text = re.sub(
                r"<\s*think\s*>.*?<\s*/\s*think\s*>",
                "",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
            result = cls.parse_response(text)
            if not result.seeds():
                return None
            return result
        except Exception as e:
            logging.warning("NL query rewrite failed: %s", e)
            return None
