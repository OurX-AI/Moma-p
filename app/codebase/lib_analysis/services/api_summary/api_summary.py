import logging
from typing import List
from app.infrastructure.llms import llm_factory
from ...schemes.public_api import PublicApi


API_SUMMARY_PROMPT = """请基于如下公开接口定义，写一段便于自然语言检索的摘要。
写清：接口做什么、关键参数含义与约束、返回值含义（无则省略）、调用时可见的副作用或外部依赖（无则省略）。
约束：
- 只依据给定签名/文档/源码可推断的事实，禁止臆造背景；不确定则写「不确定」
- 用贴合接口真实职责的自然语言（中英均可），便于检索命中；禁止套用与输入无关的套话；不要只复述标识符
- 输出短段落，少用冒号字段标签；总长控制在约 180 字内
- 末行单独给出「检索词：」后跟 2～4 个中英近义词或该职责常用说法，空格分隔
"""

API_SUMMARY_SYSTEM_PROMPT = (
    "你是库接口检索摘要助手：把公开 API 总结成便于自然语言命中的说明，"
    "支撑编码 Agent 按需求检索可调用接口。"
    "用词须贴合接口真实领域与职责；禁止无关套话；禁止只堆标识符；禁止编造输入未体现的背景。"
)


def _is_stream_error_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("llm error:"):
        return True
    if t.startswith("Invalid response"):
        return True
    if "Unexpected error: max retries exceeded" in t:
        return True
    return False


class ApiSummaryService:
    """对 Lib 公开接口生成功能/参数摘要。"""

    @staticmethod
    async def summarize(api: PublicApi) -> str:
        content = ApiSummaryService._build_llm_input(api)
        try:
            llm = llm_factory.create_model()
            stream, _usage = await llm.chat_stream(
                system_prompt=API_SUMMARY_SYSTEM_PROMPT,
                user_prompt=API_SUMMARY_PROMPT,
                user_question=content,
            )
            chunks: List[str] = []
            async for chunk in stream:
                chunks.append(chunk)
            full = "".join(chunks).strip()
            if _is_stream_error_text(full):
                return ApiSummaryService.fallback_summary(api)
            return full
        except Exception as e:
            logging.error("Lib API 摘要失败 name=%s error=%s", api.display_name(), e)
            return ApiSummaryService.fallback_summary(api)

    @staticmethod
    def _build_llm_input(api: PublicApi) -> str:
        parts = [
            f"语言: {api.language}",
            f"类型: {api.kind}",
            f"名称: {api.display_name()}",
            f"签名: {api.signature}",
        ]
        if api.params:
            parts.append(f"参数: {', '.join(api.params)}")
        if api.param_types:
            parts.append(f"参数类型: {', '.join(api.param_types)}")
        if api.return_types:
            parts.append(f"返回类型: {', '.join(api.return_types)}")
        if api.docstring:
            parts.append(f"文档: {api.docstring}")
        src = (api.source_code or "").strip()
        if src:
            parts.append(f"源码:\n{src[:4000]}")
        return "\n".join(parts)

    @staticmethod
    def fallback_summary(api: PublicApi) -> str:
        doc = (api.docstring or "").strip()
        if doc:
            return f"功能：{doc[:180]}\n关键参数：{api.signature}\n返回：{'/'.join(api.return_types) or '无'}"
        return (
            f"功能：公开接口 {api.display_name()}（摘要回退）\n"
            f"关键参数：{api.signature}\n"
            f"返回：{'/'.join(api.return_types) or '无'}"
        )
