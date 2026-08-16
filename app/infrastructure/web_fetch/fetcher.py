import logging
from app.config.settings import settings
from app.infrastructure.web_fetch.firecrawl import FirecrawlFetcher, firecrawl_result_ok
from app.infrastructure.web_fetch.jina import JinaReaderFetcher, jina_result_ok
from app.infrastructure.web_fetch.static import StaticFetcher, is_useful_content
from app.infrastructure.web_fetch.tavily import TavilyExtractFetcher, tavily_result_ok
from app.infrastructure.web_fetch.schemes import FetchResult


_ALLOWED = frozenset({"static", "tavily", "firecrawl", "jina"})

def _static_result_ok(result: FetchResult) -> bool:
    return not result.get("error") and is_useful_content(result.get("text", ""))

def _result_ok(provider: str, result: FetchResult) -> bool:
    if provider == "static":
        return _static_result_ok(result)
    if provider == "tavily":
        return tavily_result_ok(result)
    if provider == "firecrawl":
        return firecrawl_result_ok(result)
    if provider == "jina":
        return jina_result_ok(result)
    return False


class WebFetcher:
    def __init__(self) -> None:
        self._static = StaticFetcher()
        self._tavily = TavilyExtractFetcher()
        self._firecrawl = FirecrawlFetcher()
        self._jina = JinaReaderFetcher()
        primary = settings.web_fetch_primary.strip().lower()
        fallback = settings.web_fetch_fallback.strip().lower()
        self._primary = primary if primary in _ALLOWED else "static"
        self._fallback = fallback if fallback in _ALLOWED and fallback != self._primary else ""

    def _fetchers(self) -> dict[str, object]:
        return {
            "static": self._static,
            "tavily": self._tavily,
            "firecrawl": self._firecrawl,
            "jina": self._jina,
        }

    def _provider_available(self, provider: str) -> bool:
        if provider == "static":
            return True
        if provider == "tavily":
            return self._tavily.available
        if provider == "firecrawl":
            return self._firecrawl.available
        if provider == "jina":
            return self._jina.available
        return False

    async def fetch(
        self,
        url: str,
        *,
        extract_mode: str = "markdown",
        max_chars: int = 50000,
    ) -> FetchResult:
        fetchers = self._fetchers()
        last_result: FetchResult = {"url": url, "error": "No fetch provider available"}
        attempted: list[str] = []

        for provider in (self._primary, self._fallback):
            if not provider or not self._provider_available(provider):
                continue
            attempted.append(provider)
            fetcher = fetchers[provider]
            result = await fetcher.fetch(
                url,
                extract_mode=extract_mode,
                max_chars=max_chars,
            )
            if _result_ok(provider, result):
                if provider != self._primary:
                    logging.info(
                        "WebFetcher fallback succeeded: primary=%s used=%s url=%s",
                        self._primary,
                        provider,
                        url,
                    )
                return result
            last_result = result

        if attempted:
            last_result = dict(last_result)
            last_result["attempted"] = attempted
        return last_result
