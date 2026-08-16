import logging
from typing import Any
import httpx
from app.config.settings import settings


_SERPER_URL = "https://google.serper.dev/search"


class SerperSearch:
    def __init__(self, timeout: float = 15.0) -> None:
        self._api_key = settings.serper_api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, *, count: int = 5) -> list[dict[str, Any]]:
        if not self.available:
            logging.warning("SerperSearch: api_key is not configured")
            return []
        n = max(1, min(count, 10))
        payload: dict[str, Any] = {"q": query, "num": n}
        if any("\u4e00" <= c <= "\u9fff" for c in query):
            payload["gl"] = "cn"
            payload["hl"] = "zh-cn"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(
                    _SERPER_URL,
                    json=payload,
                    headers={
                        "X-API-KEY": self._api_key,
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                body = response.json()
        except Exception as e:
            logging.exception("SerperSearch request failed for query=%r: %s", query, e)
            return []
        organic = body.get("organic") or []
        items: list[dict[str, Any]] = []
        for item in organic[:n]:
            items.append({
                "title": item.get("title") or "",
                "url": item.get("link") or "",
                "content": item.get("snippet") or "",
            })
        return items
