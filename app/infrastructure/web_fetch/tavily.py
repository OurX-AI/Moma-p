import asyncio
import logging
from typing import Any
from tavily import TavilyClient
from app.config.settings import settings
from app.infrastructure.web_fetch.static import is_useful_content
from app.infrastructure.web_fetch.schemes import FetchResult


class TavilyExtractFetcher:
    def __init__(self) -> None:
        self._api_key = settings.tavily_api_key
        self._client: TavilyClient | None = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> TavilyClient:
        if self._client is None:
            self._client = TavilyClient(api_key=self._api_key)
        return self._client

    async def fetch(
        self,
        url: str,
        *,
        extract_mode: str = "markdown",
        max_chars: int = 50000,
    ) -> FetchResult:
        if not self.available:
            return {"url": url, "error": "Tavily API key is not configured"}
        try:
            client = self._get_client()
            response: dict[str, Any] = await asyncio.to_thread(
                client.extract,
                urls=[url],
                extract_depth="advanced",
            )
            results = response.get("results") or []
            if not results:
                failed = response.get("failed_results") or []
                if failed:
                    reason = failed[0].get("error") or "extract failed"
                    return {"url": url, "error": reason}
                return {"url": url, "error": "Tavily extract returned no results"}
            item = results[0]
            text = item.get("raw_content") or item.get("content") or ""
            if extract_mode == "markdown" and item.get("markdown"):
                text = item["markdown"]
            title = item.get("title") or ""
            if title and not text.startswith("#"):
                text = f"# {title}\n\n{text}"
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return {
                "url": url,
                "final_url": item.get("url") or url,
                "status": 200,
                "extractor": "tavily",
                "truncated": truncated,
                "length": len(text),
                "text": text,
            }
        except Exception as e:
            logging.exception("TavilyExtractFetcher failed for %s: %s", url, e)
            return {"url": url, "error": str(e)}


def tavily_result_ok(result: FetchResult) -> bool:
    return not result.get("error") and is_useful_content(result.get("text", ""))
