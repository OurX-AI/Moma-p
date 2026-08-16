import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional, Tuple, Union
import tiktoken
from app.infrastructure.llms.chat_models.base import ContextOverflowError

encoder = tiktoken.get_encoding("cl100k_base")


def num_tokens_from_string(texts: Union[str, List[str]]) -> int:
    """Returns the number of tokens in a text string."""
    try:
        if isinstance(texts, str):
            return len(encoder.encode(texts))
        return sum(len(encoder.encode(text)) for text in texts)
    except Exception:
        return 0


def truncate(string: str, max_len: int) -> str:
    """turns truncated text if the length of text exceed max_lenRe."""
    return encoder.decode(encoder.encode(string)[:max_len])


def is_llm_response_failed(resp: object) -> bool:
    if resp is None:
        return True
    success = getattr(resp, "success", None)
    if success is False:
        return True
    return False


async def call_with_llm_fallback(
    model_pairs: List[Tuple[str, str]],
    fn: Callable[[Any], Awaitable[Any]],
) -> Tuple[Any, Any]:
    """按 model_pairs 顺序创建 LLM 调用 fn(llm)，失败时切换下一个。返回 (fn 结果, 成功的 llm 实例)。"""
    from app.infrastructure.llms.chat_models.factory import llm_factory
    if not model_pairs:
        model_pairs = [("", "")]
    last_error: Optional[Exception] = None
    for i, (provider, model) in enumerate(model_pairs):
        llm = llm_factory.create_model(provider=provider or None, model=model or None)
        try:
            result = await fn(llm)
            if i > 0:
                logging.info("LLM fallback succeeded: %s/%s", provider, model)
            return result, llm
        except asyncio.CancelledError:
            raise
        except ContextOverflowError:
            raise
        except Exception as e:
            last_error = e
            if i >= len(model_pairs) - 1:
                raise
            logging.warning(
                "LLM call failed (%s/%s), retry with fallback: %s",
                provider,
                model,
                e,
            )
    if last_error:
        raise last_error
    raise RuntimeError("call_with_llm_fallback: no model pairs")
