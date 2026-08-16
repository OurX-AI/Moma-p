import re
import logging
from typing import List
from .model import ContentType
from app.infrastructure.llms import llm_factory


_SUMMARY_COMMON_RULES = """约束：
- 只依据给定源码可推断的事实，禁止臆造背景；不确定则写「不确定」
- 用贴合源码真实职责的自然语言（中英均可），便于检索命中；禁止套用与源码无关的套话；不要只复述标识符
- 输出短段落，少用冒号字段标签；总长控制在约 180 字内
- 末行单独给出「检索词：」后跟 2～4 个中英近义词或该职责常用说法，空格分隔；禁止无关业务套话
"""

FUNCTION_SUMMARY_PROMPT = f"""请基于如下函数/方法定义，写一段便于自然语言检索的摘要。
写清：做什么、何时会改到它、关键参数、可见的副作用或外部依赖（I/O、网络、锁、日志等；无则省略）。
{_SUMMARY_COMMON_RULES}"""

CLASS_SUMMARY_PROMPT = f"""请基于如下类定义，写一段便于自然语言检索的摘要。
写清：职责边界、何时会改到它、关键属性与核心方法（挑最重要的，勿逐条罗列）。
{_SUMMARY_COMMON_RULES}"""

STRUCT_SUMMARY_PROMPT = f"""请基于如下结构体定义，写一段便于自然语言检索的摘要。
写清：承载的数据/职责、何时会改到它、关键字段与相关方法（挑最重要的）。
{_SUMMARY_COMMON_RULES}"""

INTERFACE_SUMMARY_PROMPT = f"""请基于如下接口定义，写一段便于自然语言检索的摘要。
写清：契约职责、何时会改到它、关键方法（挑最重要的）。
{_SUMMARY_COMMON_RULES}"""

FILE_SUMMARY_PROMPT = f"""请基于如下源码文件，写一段便于自然语言检索的摘要。
写清：文件职责、何时会改到它、与外部模块的关键协作（可见则写）。
{_SUMMARY_COMMON_RULES}"""

FOLDER_SUMMARY_PROMPT = f"""请基于如下文件夹（模块）中子项功能描述，写一段便于自然语言检索的摘要。
写清：模块职责、何时会改到该模块。
{_SUMMARY_COMMON_RULES}"""

SYMBOL_SUMMARY_SYSTEM_PROMPT = (
    "你是代码检索摘要助手：把符号/文件总结成便于自然语言命中的功能描述，"
    "支撑「用自然语言找该改哪个符号或文件」。"
    "用词须贴合源码真实领域与职责（业务、框架、协议、基础设施均可）；"
    "禁止无关套话；禁止只堆标识符；禁止编造源码未体现的背景。"
)

_DOCSTRING_RE = re.compile(
    r'^\s*(?:[ruRU]{0,2})("""|\'\'\')(.*?)\1',
    re.DOTALL,
)
_SIG_RE = re.compile(
    r"^\s*(?:(?:async\s+)?def|class|func|interface|struct|type)\s+[^\n{;]+",
    re.MULTILINE,
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


def _strip_think_tags(text: str) -> str:
    if not text:
        return ""
    pattern = r"<\s*think\s*>.*?<\s*/\s*think\s*>"
    return re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL).strip()


class CodeSummary:
    """符号/文件摘要：优先 LLM，失败时确定性回退以保证可嵌入检索。"""

    @staticmethod
    def fallback_summary(content: str, content_type: ContentType) -> str:
        """LLM 不可用时的确定性摘要：签名 + docstring + 源码预览。"""
        src = (content or "").strip()
        if not src:
            return ""
        kind_label = {
            ContentType.FILE: "文件",
            ContentType.CLASS: "类",
            ContentType.STRUCT: "结构体",
            ContentType.INTERFACE: "接口",
            ContentType.FUNCTION: "函数",
            ContentType.FOLDER: "模块",
            ContentType.CODE_CHUNK: "代码块",
        }.get(content_type, "符号")

        sig = ""
        m_sig = _SIG_RE.search(src)
        if m_sig:
            sig = " ".join(m_sig.group(0).split())[:200]

        doc = ""
        # 常见：签名后紧跟 docstring
        after_sig = src[m_sig.end() :] if m_sig else src
        m_doc = _DOCSTRING_RE.search(after_sig[:1200])
        if m_doc:
            doc = " ".join(m_doc.group(2).strip().split())[:180]
        if not doc:
            for ln in src.splitlines()[:12]:
                s = ln.strip()
                if s.startswith("#") and len(s) > 2:
                    doc = s.lstrip("#").strip()[:180]
                    break
                if s.startswith("//") and len(s) > 3:
                    doc = s[2:].strip()[:180]
                    break

        lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
        preview = " ".join(lines[:4])[:240]

        parts = [f"功能：{kind_label}摘要（回退）"]
        if doc:
            parts[0] = f"功能：{doc}"
        if sig:
            parts.append(f"签名：{sig}")
        parts.append(f"场景：检索定位相关实现时可能命中")
        if preview and not doc:
            parts.append(f"预览：{preview}")
        return "\n".join(parts)

    @staticmethod
    async def llm_summarize(content: str, content_type: ContentType) -> str:
        """使用 LLM 生成代码内容摘要；失败则确定性回退。"""
        fallback = CodeSummary.fallback_summary(content, content_type)
        try:
            if content_type == ContentType.FILE:
                user_prompt = FILE_SUMMARY_PROMPT
            elif content_type == ContentType.CLASS:
                user_prompt = CLASS_SUMMARY_PROMPT
            elif content_type == ContentType.FUNCTION:
                user_prompt = FUNCTION_SUMMARY_PROMPT
            elif content_type == ContentType.STRUCT:
                user_prompt = STRUCT_SUMMARY_PROMPT
            elif content_type == ContentType.INTERFACE:
                user_prompt = INTERFACE_SUMMARY_PROMPT
            elif content_type == ContentType.FOLDER:
                user_prompt = FOLDER_SUMMARY_PROMPT
            else:
                return fallback

            llm = llm_factory.create_model()
            stream, _usage = await llm.chat_stream(
                system_prompt=SYMBOL_SUMMARY_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                user_question=content,
            )
            chunks: List[str] = []
            async for chunk in stream:
                chunks.append(chunk)
            full = "".join(chunks)
            if _is_stream_error_text(full):
                return fallback

            result = _strip_think_tags(full).strip()
            return result or fallback

        except Exception as e:
            logging.error("生成%s摘要失败，使用回退: %s", content_type, e)
            return fallback
