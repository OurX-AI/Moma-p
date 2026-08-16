import logging
from typing import Any
import httpx
from app.config.settings import settings


class BraveSearch:
    """Brave Web Search API 封装."""
    _BASE_URL="https://api.search.brave.com/res/v1/web/search"

    def __init__(self,timeout:float=10.0)->None:
        self.api_key=settings.brave_api_key
        self.timeout=timeout

    async def search(self,query:str,*,count:int=5)->list[dict[str,Any]]:
        """使用 Brave Web Search 搜索，返回统一结构的结果列表."""
        if not self.api_key:
            logging.warning("BraveSearch: api_key is not configured")
            return []

        n=max(1,min(count,10))

        try:
            async with httpx.AsyncClient(timeout=self.timeout,follow_redirects=True) as client:
                resp=await client.get(
                    self._BASE_URL,
                    params={"q":query,"count":n},
                    headers={
                        "Accept":"application/json",
                        "X-Subscription-Token":self.api_key,
                    },
                    timeout=self.timeout
                )
                resp.raise_for_status()
                payload=resp.json()
        except Exception as e:
            logging.exception("BraveSearch request failed: %s",e)
            return []

        web=payload.get("web") or {}
        results=web.get("results") or []

        items: list[dict[str, Any]] = []
        for item in results[:n]:
            items.append({
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "content": item.get("description") or "",
            })
        return items
