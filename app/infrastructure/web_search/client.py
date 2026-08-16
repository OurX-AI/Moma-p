import logging
from typing import Any
from app.config.settings import settings
from app.infrastructure.web_search.brave import BraveSearch
from app.infrastructure.web_search.duckduckgo import DuckDuckGoSearch
from app.infrastructure.web_search.serper import SerperSearch
from app.infrastructure.web_search.tavily import TavilySearch

_ALLOWED = frozenset({"tavily", "brave", "serper", "duckduckgo"})


class WebSearchClient:
    def __init__(self) -> None:
        self._tavily = TavilySearch()
        self._brave = BraveSearch()
        self._serper = SerperSearch()
        self._duckduckgo = DuckDuckGoSearch()
        primary = settings.web_search_primary.strip().lower()
        fallback = settings.web_search_fallback.strip().lower()
        self._primary = primary if primary in _ALLOWED else "tavily"
        self._fallback = fallback if fallback in _ALLOWED and fallback != self._primary else ""

    def _provider_available(self, provider: str) -> bool:
        if provider == "tavily":
            return bool(settings.tavily_api_key)
        if provider == "brave":
            return bool(settings.brave_api_key)
        if provider == "serper":
            return bool(settings.serper_api_key)
        if provider == "duckduckgo":
            return True
        return False

    async def _search_with(self, provider: str, query: str, *, count: int) -> list[dict[str, Any]]:
        if provider == "tavily":
            return await self._tavily.search(query, count=count)
        if provider == "brave":
            return await self._brave.search(query, count=count)
        if provider == "serper":
            return await self._serper.search(query, count=count)
        if provider == "duckduckgo":
            return await self._duckduckgo.search(query, count=count)
        return []

    async def search(self, query: str, *, count: int = 5) -> tuple[list[dict[str, Any]], str, bool]:
        attempted = False
        last_provider = ""
        for provider in (self._primary, self._fallback):
            if not provider or not self._provider_available(provider):
                continue
            attempted = True
            last_provider = provider
            results = await self._search_with(provider, query, count=count)
            if results:
                if provider != self._primary:
                    logging.info(
                        "WebSearchClient fallback succeeded: primary=%s used=%s query=%r",
                        self._primary,
                        provider,
                        query,
                    )
                return results, provider, True
        return [], last_provider or self._primary, attempted
