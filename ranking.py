"""Result fusion and diversification."""
from urllib.parse import urlsplit


def norm_url(url: str) -> str:
    p = urlsplit(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}"


def diversify(items: list[dict], per_domain: int = 2, window: int = 10) -> list[dict]:
    """Within the first `window` slots allow at most `per_domain` results per domain; push the rest down."""
    head, tail, counts = [], [], {}
    for it in items:
        d = it.get("domain") or norm_url(it["url"]).split("/")[0]
        if len(head) < window and counts.get(d, 0) < per_domain:
            head.append(it)
            counts[d] = counts.get(d, 0) + 1
        else:
            tail.append(it)
    return head + tail


def rrf_fuse(lists: list[list[dict]], weights: list[float] | None = None, k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion across ranked lists (local index + external providers)."""
    weights = weights or [1.0] * len(lists)
    fused: dict[str, dict] = {}
    for lst, w in zip(lists, weights):
        for rank, item in enumerate(lst):
            key = norm_url(item["url"])
            entry = fused.setdefault(key, {"item": dict(item), "score": 0.0, "sources": set()})
            entry["score"] += w / (k + rank + 1)
            entry["sources"].add(item.get("source", "web"))
            # prefer the richer record (thumbnail / longer snippet)
            if len(item.get("snippet", "")) > len(entry["item"].get("snippet", "")):
                entry["item"]["snippet"] = item["snippet"]
            if not entry["item"].get("thumbnail") and item.get("thumbnail"):
                entry["item"]["thumbnail"] = item["thumbnail"]
    out = []
    for e in sorted(fused.values(), key=lambda e: e["score"], reverse=True):
        it = e["item"]
        it["source"] = "mave+web" if len(e["sources"]) > 1 else next(iter(e["sources"]))
        out.append(it)
    return diversify(out, per_domain=2, window=10)
