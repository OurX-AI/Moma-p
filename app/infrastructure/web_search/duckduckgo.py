import asyncio
import logging
from typing import Any
from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import DuckDuckGoSearchException


class DuckDuckGoSearch:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def _region(query: str) -> str:
        if any("\u4e00" <= c <= "\u9fff" for c in query):
            return "cn-zh"
        return "wt-wt"

    def _search_sync(self, query: str, count: int) -> list[dict[str, Any]]:
        timeout = max(1, int(self.timeout))
        results = DDGS(timeout=timeout).text(
            query,
            max_results=count,
            region=self._region(query),
        )
        items: list[dict[str, Any]] = []
        for item in results or []:
            items.append({
                "title": item.get("title") or "",
                "url": item.get("href") or "",
                "content": item.get("body") or "",
            })
        return items

    async def search(self, query: str, *, count: int = 5) -> list[dict[str, Any]]:
        n = max(1, min(count, 10))
        try:
            return await asyncio.to_thread(self._search_sync, query, n)
        except DuckDuckGoSearchException as e:
            logging.warning("DuckDuckGoSearch failed for query=%r: %s", query, e)
            return []
        except Exception as e:
            logging.exception("DuckDuckGoSearch failed for query=%r: %s", query, e)
            return []
