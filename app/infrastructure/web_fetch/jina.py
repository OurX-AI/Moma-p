import logging
from typing import Any
import httpx
from app.config.settings import settings
from app.infrastructure.web_fetch.static import is_useful_content
from app.infrastructure.web_fetch.schemes import FetchResult


_JINA_READER_URL = "https://r.jina.ai/"


class JinaReaderFetcher:
    def __init__(self, timeout: float = 60.0) -> None:
        self._api_key = settings.jina_api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return True

    def _headers(self, extract_mode: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Engine": "browser",
            "X-Return-Format": "markdown" if extract_mode == "markdown" else "text",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def fetch(
        self,
        url: str,
        *,
        extract_mode: str = "markdown",
        max_chars: int = 50000,
    ) -> FetchResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(
                    _JINA_READER_URL,
                    json={"url": url},
                    headers=self._headers(extract_mode),
                )
                response.raise_for_status()
                body = response.json()
            if body.get("code") and body.get("code") != 200:
                message = body.get("message") or body.get("readableMessage") or "Jina Reader request failed"
                return {"url": url, "error": message}
            data: dict[str, Any] = body.get("data") or {}
            if not data and isinstance(body.get("content"), str):
                data = body
            text = data.get("content") or ""
            title = data.get("title") or ""
            if title and text and not text.startswith("#"):
                text = f"# {title}\n\n{text}"
            final_url = data.get("url") or url
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return {
                "url": url,
                "final_url": final_url,
                "status": 200,
                "extractor": "jina",
                "truncated": truncated,
                "length": len(text),
                "text": text,
            }
        except Exception as e:
            logging.exception("JinaReaderFetcher failed for %s: %s", url, e)
            return {"url": url, "error": str(e)}


def jina_result_ok(result: FetchResult) -> bool:
    return not result.get("error") and is_useful_content(result.get("text", ""))
