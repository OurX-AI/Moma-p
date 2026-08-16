import json
from typing import Any, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from app.infrastructure.web_fetch import WebFetcher, validate_fetch_url
from .web_fetch_extract import WebFetchLlmExtractor


@register_tool(name="web_fetch", toolset="web")
class WebFetchTool(BaseTool):
    """抓取 URL；可选按 prompt 用会话模型二次抽取。"""

    def __init__(self, max_chars: int = 50000, agent_ctx: AgentContext | None = None) -> None:
        super().__init__(agent_ctx=agent_ctx)
        self.max_chars = max_chars
        self._fetcher = WebFetcher()

    @property
    def name(self) -> str:
        return "web_fetch"

    def description(self, params=None) -> str:
        if WebFetchLlmExtractor.is_enabled():
            return """Fetch a URL and extract information with a prompt (HTML → markdown, then LLM extract).

When to use:
- Known documentation/API URLs; provide `prompt` describing what to extract.
- Prefer MCP web-fetch tools when they are already activated and better suited.

When NOT to use:
- Open-ended discovery (use `web_search` first).
- Interactive SPA clicks/forms (use `browser`).

Failure recovery:
- Empty/blocked page -> try alternate URL from search, or `browser` if JS-rendered.
- Extract too broad -> tighten `prompt` / reduce `maxChars` and retry."""
        return """Fetch a URL and extract readable content (HTML → markdown/text).

When to use:
- Known documentation/API URLs for static pages.

When NOT to use:
- Open-ended discovery (use `web_search` first).
- Interactive SPA clicks/forms (use `browser`).

Failure recovery:
- Empty/blocked page -> try alternate URL, or `browser` if JS-rendered.
- Too much noise -> switch extractMode or lower maxChars."""

    @property
    def parameters(self) -> dict[str, Any]:
        props: dict[str, Any] = {
            "url": {
                "type": "string",
                "description": "URL to fetch",
            },
            "extractMode": {
                "type": "string",
                "enum": ["markdown", "text"],
                "default": "markdown",
                "description": "Extraction mode for HTML pages before optional LLM extract",
            },
            "maxChars": {
                "type": "integer",
                "minimum": 100,
                "description": "Maximum characters of page content before LLM extract / return",
            },
        }
        required = ["url"]
        if WebFetchLlmExtractor.is_enabled():
            props["prompt"] = {
                "type": "string",
                "description": "What to extract from the page (required when LLM extract is enabled)",
            }
            required.append("prompt")
        return {
            "type": "object",
            "properties": props,
            "required": required,
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        url: str,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        prompt: Optional[str] = None,
    ) -> ToolResult:
        llm_extract = WebFetchLlmExtractor.is_enabled()
        if llm_extract and not (prompt or "").strip():
            return ToolErrorResult(
                json.dumps(
                    {
                        "error": "prompt is required when WEB_FETCH_LLM_EXTRACT is enabled",
                        "url": url,
                    },
                    ensure_ascii=False,
                )
            )

        max_chars = maxChars or self.max_chars

        is_valid, error_msg = validate_fetch_url(url)
        if not is_valid:
            return ToolErrorResult(
                json.dumps(
                    {"error": f"URL validation failed: {error_msg}", "url": url},
                    ensure_ascii=False,
                )
            )

        result = await self._fetcher.fetch(
            url,
            extract_mode=extractMode,
            max_chars=max_chars,
        )

        if result.get("error") and not result.get("text"):
            return ToolErrorResult(
                json.dumps(
                    {
                        "error": result["error"],
                        "url": url,
                        "attempted": result.get("attempted"),
                    },
                    ensure_ascii=False,
                )
            )

        page_text = result.get("text", "") or ""
        payload: dict[str, Any] = {
            "url": result.get("url", url),
            "finalUrl": result.get("final_url", url),
            "status": result.get("status", 0),
            "extractor": result.get("extractor", "unknown"),
            "truncated": result.get("truncated", False),
            "length": result.get("length", len(page_text)),
        }
        if result.get("attempted"):
            payload["attempted"] = result["attempted"]

        if not llm_extract:
            payload["text"] = page_text
            return ToolSuccessResult(json.dumps(payload, ensure_ascii=False))

        prompt_text = (prompt or "").strip()
        payload["prompt"] = prompt_text
        try:
            extracted = await WebFetchLlmExtractor.extract(
                agent_ctx=agent_ctx,
                markdown_content=page_text,
                prompt=prompt_text,
            )
        except LookupError:
            payload["text"] = page_text
            payload["llmExtract"] = False
            payload["extractFallback"] = "invalid_model"
            return ToolSuccessResult(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            return ToolErrorResult(
                json.dumps(
                    {
                        "error": f"LLM extract failed: {exc}",
                        "url": payload.get("url", url),
                        "extractor": payload.get("extractor"),
                    },
                    ensure_ascii=False,
                )
            )

        payload["llmExtract"] = True
        payload["text"] = extracted
        payload["length"] = len(extracted)
        return ToolSuccessResult(json.dumps(payload, ensure_ascii=False))