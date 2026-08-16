from __future__ import annotations
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
from app.config.settings import settings
from app.infrastructure.llms import llm_factory
from app.infrastructure.llms.chat_models.base import ContextOverflowError
from .code_summary import (
    SYMBOL_SUMMARY_SYSTEM_PROMPT,
    _SUMMARY_COMMON_RULES,
    CodeSummary,
    _is_stream_error_text,
    _strip_think_tags,
)
from .model import ContentType


@dataclass(frozen=True)
class SymbolSummaryRequest:
    """单条待摘要符号。"""

    source: str
    content_type: ContentType
    name: str = ""


class SymbolBatchSummarizer:
    """同文件多符号一次 LLM 调用批量摘要。

    失败策略：
    - context overflow / 判定超长 → 折半再试，直至单条
    - 其它 LLM/JSON 失败 → 该批回退单条
    - 部分缺 id → 仅对缺失项补跑单条
    """

    _JSON_ARRAY = re.compile(r"\[[\s\S]*\]")
    _TYPE_LABEL = {
        ContentType.FUNCTION: "函数/方法",
        ContentType.CLASS: "类",
        ContentType.STRUCT: "结构体",
        ContentType.INTERFACE: "接口",
        ContentType.FILE: "文件",
        ContentType.FOLDER: "模块",
    }
    _BATCH_SYSTEM = (
        SYMBOL_SUMMARY_SYSTEM_PROMPT
        + " 本次输入含多个符号，只输出合法 JSON 数组，不要 markdown 代码块。"
    )
    _BATCH_USER_PROMPT = f"""对下列每个符号分别写便于自然语言检索的摘要。
每个摘要写清：做什么、何时会改到它；函数/方法另含关键参数与可见副作用（无则省略）；类另含职责与核心方法。
{_SUMMARY_COMMON_RULES}
输出严格 JSON 数组，元素格式：{{"id": <整数>, "summary": "<摘要文本>"}}
必须覆盖输入中的每一个 id，id 与输入编号一致。
"""

    @classmethod
    async def summarize_many(
        cls,
        requests: Sequence[SymbolSummaryRequest],
        *,
        batch_size: Optional[int] = None,
        concurrency: Optional[int] = None,
    ) -> List[str]:
        """批量摘要；返回与 requests 等长的摘要列表。"""
        items = list(requests or [])
        if not items:
            return []
        size = batch_size
        if size is None:
            size = int(settings.code_analysis_symbol_summary_llm_batch_size or 1)
        size = max(1, size)

        conc = concurrency
        if conc is None:
            conc = int(settings.code_analysis_symbol_summary_llm_concurrency or 1)
        conc = max(1, conc)

        try:
            if size <= 1 or len(items) <= 1:
                return await cls._summarize_singles(items, concurrency=conc)

            batches: List[List[Tuple[int, SymbolSummaryRequest]]] = []
            indexed = list(enumerate(items))
            for start in range(0, len(indexed), size):
                batches.append(indexed[start : start + size])

            out: List[Optional[str]] = [None] * len(items)
            sem = asyncio.Semaphore(conc)

            async def run_batch(batch: List[Tuple[int, SymbolSummaryRequest]]) -> None:
                async with sem:
                    summaries = await cls._summarize_one_batch([req for _, req in batch])
                    for (idx, _), text in zip(batch, summaries):
                        out[idx] = text

            await asyncio.gather(*[run_batch(b) for b in batches])
            return [t or "" for t in out]
        
        except Exception as e:
            raise RuntimeError(f"批量符号摘要失败 requests={requests} error={e}") from e

    @classmethod
    async def _summarize_singles(
        cls,
        items: Sequence[SymbolSummaryRequest],
        *,
        concurrency: int,
    ) -> List[str]:
        try:
            sem = asyncio.Semaphore(max(1, concurrency))

            async def one(req: SymbolSummaryRequest) -> str:
                async with sem:
                    return await CodeSummary.llm_summarize(req.source, req.content_type)

            return list(await asyncio.gather(*[one(r) for r in items]))
        
        except Exception as e:
            raise RuntimeError(f"单条符号摘要失败 items={items} error={e}") from e

    @classmethod
    async def _retry_by_halves(
        cls,
        items: Sequence[SymbolSummaryRequest],
    ) -> List[str]:
        """超长时折半重试，最终落到单条。"""
        n = len(items)
        if n <= 1:
            return await cls._summarize_singles(items, concurrency=1)
        mid = max(1, n // 2)
        logging.warning("批量符号摘要超长，折半重试 n=%s -> %s+%s", n, mid, n - mid)
        left = await cls._summarize_one_batch(list(items[:mid]))
        right = await cls._summarize_one_batch(list(items[mid:]))
        return left + right

    @classmethod
    async def _summarize_one_batch(cls, items: Sequence[SymbolSummaryRequest]) -> List[str]:
        if len(items) == 1:
            return [await CodeSummary.llm_summarize(items[0].source, items[0].content_type)]
        try:
            llm = llm_factory.create_model()
            stream, _usage = await llm.chat_stream(
                system_prompt=cls._BATCH_SYSTEM,
                user_prompt=cls._BATCH_USER_PROMPT,
                user_question=cls.build_batch_user_question(items),
            )
            chunks: List[str] = []
            async for chunk in stream:
                chunks.append(chunk)
            full = "".join(chunks)
            if _is_stream_error_text(full):
                logging.warning("批量符号摘要 LLM 错误文本，回退单条 n=%s", len(items))
                return await cls._summarize_singles(items, concurrency=1)
            parsed = cls.parse_batch_response(_strip_think_tags(full), expected=len(items))
            if parsed is None:
                logging.warning("批量符号摘要 JSON 解析失败，回退单条 n=%s", len(items))
                return await cls._summarize_singles(items, concurrency=1)
            result: List[str] = []
            missing: List[Tuple[int, SymbolSummaryRequest]] = []
            for i, req in enumerate(items):
                text = (parsed.get(i) or "").strip()
                if text:
                    result.append(text)
                else:
                    result.append("")
                    missing.append((i, req))
            if missing:
                logging.warning(
                    "批量符号摘要缺 id，补跑单条 missing=%s/%s",
                    len(missing),
                    len(items),
                )
                filled = await cls._summarize_singles(
                    [req for _, req in missing],
                    concurrency=1,
                )
                for (idx, _), text in zip(missing, filled):
                    result[idx] = text
            return result
        except ContextOverflowError:
            return await cls._retry_by_halves(items)
        except Exception as e:
            logging.error("批量符号摘要失败，回退单条: %s", e)
            return await cls._summarize_singles(items, concurrency=1)

    @classmethod
    def build_batch_user_question(
        cls,
        items: Sequence[SymbolSummaryRequest],
        *,
        max_chars_per_symbol: int = 2500,
    ) -> str:
        parts: List[str] = []
        limit = max(200, int(max_chars_per_symbol))
        for i, req in enumerate(items):
            label = cls._TYPE_LABEL.get(req.content_type, "符号")
            name = (req.name or "").strip() or f"item_{i}"
            src = (req.source or "").strip()
            if len(src) > limit:
                src = src[:limit] + "\n...[truncated]..."
            parts.append(f"### id={i} type={label} name={name}\n{src}")
        return "\n\n".join(parts)

    @classmethod
    def parse_batch_response(
        cls,
        text: str,
        *,
        expected: int,
    ) -> Optional[dict[int, str]]:
        """解析批量 JSON；成功返回 id->summary，失败返回 None。"""
        raw = (text or "").strip()
        if not raw:
            return None
        m = cls._JSON_ARRAY.search(raw)
        blob = m.group(0) if m else raw
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list) or not data:
            return None
        out: dict[int, str] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                idx = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= expected:
                continue
            summary = str(row.get("summary") or "").strip()
            if summary:
                out[idx] = summary
        if not out:
            return None
        return out
