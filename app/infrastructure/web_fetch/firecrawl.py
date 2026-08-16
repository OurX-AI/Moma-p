import logging
from typing import Any
import httpx
from app.config.settings import settings
from app.infrastructure.web_fetch.static import is_useful_content
from app.infrastructure.web_fetch.schemes import FetchResult


_FIRECRAWL_URL = "https://api.firecrawl.dev/v1/scrape"


class FirecrawlFetcher:
    def __init__(self, timeout: float = 60.0) -> None:
        self._api_key = settings.firecrawl_api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def fetch(
        self,
        url: str,
        *,
        extract_mode: str = "markdown",
        max_chars: int = 50000,
    ) -> FetchResult:
        if not self.available:
            return {"url": url, "error": "Firecrawl API key is not configured"}
        try:
            payload: dict[str, Any] = {
                "url": url,
                "formats": ["markdown"] if extract_mode == "markdown" else ["markdown", "rawHtml"],
            }
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(
                    _FIRECRAWL_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                body = response.json()
            if not body.get("success"):
                message = body.get("error") or body.get("message") or "Firecrawl scrape failed"
                return {"url": url, "error": message}
            data = body.get("data") or {}
            text = data.get("markdown") or data.get("content") or ""
            if not text and data.get("rawHtml"):
                text = data["rawHtml"]
            metadata = data.get("metadata") or {}
            title = metadata.get("title") or ""
            if title and not text.startswith("#"):
                text = f"# {title}\n\n{text}"
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return {
                "url": url,
                "final_url": metadata.get("sourceURL") or metadata.get("url") or url,
                "status": metadata.get("statusCode") or 200,
                "extractor": "firecrawl",
                "truncated": truncated,
                "length": len(text),
                "text": text,
            }
        except Exception as e:
            logging.exception("FirecrawlFetcher failed for %s: %s", url, e)
            return {"url": url, "error": str(e)}


def firecrawl_result_ok(result: FetchResult) -> bool:
    return not result.get("error") and is_useful_content(result.get("text", ""))
