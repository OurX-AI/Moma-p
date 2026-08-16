import asyncio
import logging
from tavily import TavilyClient
from app.config.settings import settings


class TavilySearch:
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

    async def search(self, query: str, *, count: int = 6) -> list[dict]:
        if not self.available:
            return []
        n = max(1, min(count, 10))
        try:
            client = self._get_client()
            response = await asyncio.to_thread(
                client.search,
                query=query,
                search_depth="advanced",
                max_results=n,
            )
            return [
                {
                    "url": res["url"],
                    "title": res["title"],
                    "content": res["content"],
                    "score": res["score"],
                }
                for res in response["results"]
            ]
        except Exception as e:
            logging.exception("TavilySearch failed for query=%r: %s", query, e)
        return []

    """
    async def retrieve_chunks(self, question):
        chunks = []
        aggs = []
        logging.info("[Tavily]Q: " + question)
        search_results = await self.search(question)
        for r in search_results:
            id = str(uuid.uuid4()).replace("-", "")
            chunks.append({
                "chunk_id": id,
                "content_ltks": rag_tokenizer.tokenize(r["content"]),
                "content_with_weight": r["content"],
                "doc_id": "",
                "docnm_kwd": r["title"],
                "kb_id": "",
                "important_kwd": [],
                "image_id": "",
                "similarity": r["score"],
                "vector_similarity": 1.,
                "term_similarity": 0,
                "vector": [],
                "positions": [],
                "url": r["url"]
            })
            aggs.append({
                "doc_name": r["title"],
                "doc_id": id,
                "count": 1,
                "url": r["url"]
            })
            logging.info("[Tavily]R: "+r["content"][:128]+"...")
        return {"chunks": chunks, "doc_aggs": aggs}
    """
