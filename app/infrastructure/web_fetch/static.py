import html
import json
import logging
import re
from typing import Any
import httpx
from readability import Document
from app.infrastructure.web_fetch.schemes import FetchResult

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5
MIN_USEFUL_CHARS = 200


def strip_tags(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def normalize_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_markdown(html_content: str) -> str:
    text = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
        lambda m: f"[{strip_tags(m[2])}]({m[1]})",
        html_content,
        flags=re.I,
    )
    text = re.sub(
        r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
        lambda m: f'\n{"#" * int(m[1])} {strip_tags(m[2])}\n',
        text,
        flags=re.I,
    )
    text = re.sub(
        r"<li[^>]*>([\s\S]*?)</li>",
        lambda m: f"\n- {strip_tags(m[1])}",
        text,
        flags=re.I,
    )
    text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
    return normalize_text(strip_tags(text))


def is_useful_content(text: str) -> bool:
    return len((text or "").strip()) >= MIN_USEFUL_CHARS


class StaticFetcher:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    async def fetch(
        self,
        url: str,
        *,
        extract_mode: str = "markdown",
        max_chars: int = 50000,
    ) -> FetchResult:
        client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=self.timeout,
        )
        try:
            return await self._fetch(client, url, extract_mode, max_chars)
        finally:
            await client.aclose()

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        extract_mode: str,
        max_chars: int,
    ) -> FetchResult:
        try:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            ctype = response.headers.get("content-type", "")
            if "application/json" in ctype:
                text = json.dumps(response.json(), indent=2, ensure_ascii=False)
                extractor = "json"
            elif "text/html" in ctype or response.text[:256].lower().startswith(
                ("<!doctype", "<html")
            ):
                doc = Document(response.text)
                content = (
                    to_markdown(doc.summary())
                    if extract_mode == "markdown"
                    else strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text = response.text
                extractor = "raw"
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]
            return {
                "url": url,
                "final_url": str(response.url),
                "status": response.status_code,
                "extractor": f"static/{extractor}",
                "truncated": truncated,
                "length": len(text),
                "text": text,
            }
        except Exception as e:
            logging.exception("StaticFetcher failed for %s: %s", url, e)
            return {"url": url, "error": str(e)}
