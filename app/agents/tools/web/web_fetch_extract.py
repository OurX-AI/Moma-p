from typing import Any, List, Tuple
from app.agents.schemes import AgentContext
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.infrastructure.llms.utils import (
    call_with_llm_fallback,
    is_llm_response_failed,
)


class WebFetchLlmExtractor:
    """按 prompt 二次抽取；直接用 AgentContext.llms_list + call_with_llm_fallback。"""

    MAX_CONTENT_CHARS = 100_000
    _SYSTEM = (
        "You extract information from web page content. "
        "Answer only from the provided content. Be concise."
    )

    @staticmethod
    def is_enabled() -> bool:
        from app.config.settings import settings
        return bool(getattr(settings, "web_fetch_llm_extract", True))

    @classmethod
    def usable_llms_list(cls, agent_ctx: AgentContext) -> List[Tuple[str, str]]:
        """过滤出可用模型对；全无效则 LookupError（上层回退原文）。"""
        usable: List[Tuple[str, str]] = []
        for item in agent_ctx.llms_list or []:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            provider = str(item[0] or "").strip()
            model = str(item[1] or "").strip()
            if not provider and not model:
                default_p, default_m = llm_factory.get_default_model()
                if (
                    default_p
                    and default_m
                    and llm_factory.if_model_support(default_p, default_m)
                ):
                    usable.append(("", ""))
                continue
            if llm_factory.if_model_support(provider, model):
                usable.append((provider, model))
        if not usable:
            raise LookupError("invalid_model")
        return usable

    @classmethod
    def build_user_question(cls, markdown_content: str, prompt: str) -> str:
        content = markdown_content or ""
        if len(content) > cls.MAX_CONTENT_CHARS:
            content = (
                content[: cls.MAX_CONTENT_CHARS]
                + "\n\n[Content truncated due to length...]"
            )
        return (
            "Web page content:\n"
            "---\n"
            f"{content}\n"
            "---\n\n"
            f"{prompt}\n\n"
            "Provide a concise response based only on the content above. "
            "Use quotation marks for exact wording; keep quotes short."
        )

    @classmethod
    async def extract(
        cls,
        *,
        agent_ctx: AgentContext,
        markdown_content: str,
        prompt: str,
    ) -> str:
        prompt_text = (prompt or "").strip()
        if not prompt_text:
            raise ValueError("prompt is required when WEB_FETCH_LLM_EXTRACT is enabled")

        pairs = cls.usable_llms_list(agent_ctx)
        user_question = cls.build_user_question(markdown_content, prompt_text)

        async def _once(llm: Any) -> str:
            response, _ = await llm.chat(
                system_prompt=cls._SYSTEM,
                user_prompt="",
                user_question=user_question,
                history=[],
                temperature=0.1,
            )
            if is_llm_response_failed(response):
                detail = getattr(response, "content", None) or "llm extract failed"
                raise RuntimeError(str(detail))
            content = (getattr(response, "content", None) or "").strip()
            if not content:
                raise RuntimeError("empty extract response")
            return content

        try:
            text, _ = await call_with_llm_fallback(pairs, _once)
            return text
        except LookupError:
            raise
        except Exception as exc:
            raise LookupError(f"invalid_model: {exc}") from exc
