"""External web search provider: Brave Search (web / news / images / videos). Results are cached through
``SharedCache`` (public data, safe to share between workers). Without ``BRAVE_API_KEY`` it serves demo data in
development and raises ``ProviderError`` in production."""
import re

import httpx

from ..core.cache import build_cache
from ..core.config import settings
from ..core.errors import ProviderError

BRAVE_PATHS = {
    "web": "web/search",
    "tech": "web/search",
    "news": "news/search",
    "images": "images/search",
    "videos": "videos/search",
}
TAG = re.compile(r"<[^>]+>")
_cache = build_cache("providers", ttl=300, maxsize=512)  # public search results: safe to share between workers


def demo_results(q: str, kind: str) -> list[dict]:
    return [
        {
            "title": f"{q} - demo result {i}",
            "url": f"https://example.com/{kind}/{i}",
            "snippet": "Demo data. Set BRAVE_API_KEY on the server to get real web results.",
            "thumbnail": f"https://picsum.photos/seed/{kind}{i}/300/300",
            "source": "web",
        }
        for i in range(1, 9)
    ]


async def web_search(q: str, kind: str = "web", count: int = 20, offset: int = 0) -> list[dict]:
    if not settings.brave_key:
        if settings.is_production:  # demo data must never be served as if it were real results
            raise ProviderError("search provider is not configured")
        return demo_results(q, kind) if offset == 0 else []
    key = (q, kind, count, offset)
    hit = await _cache.aget(key)
    if hit is not None:
        return hit
    query = f"{q} technology" if kind == "tech" else q
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(
                f"https://api.search.brave.com/res/v1/{BRAVE_PATHS[kind]}",
                params={"q": query, "count": count, "offset": offset},
                headers={"X-Subscription-Token": settings.brave_key, "Accept": "application/json"},
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"search provider unreachable: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise ProviderError(f"search provider error {r.status_code}")
    data = r.json()
    raw = data.get("web", {}).get("results", []) if kind in ("web", "tech") else data.get("results", [])
    items = [
        {
            "title": TAG.sub("", x.get("title") or ""),
            "url": x.get("url") or "",
            "snippet": TAG.sub("", x.get("description") or ""),
            "thumbnail": (x.get("thumbnail") or {}).get("src"),
            "source": "web",
        }
        for x in raw
        if x.get("url")
    ]
    await _cache.aset(key, items)
    return items