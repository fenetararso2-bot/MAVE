"""sitemap.xml discovery and parsing (sitemap index, gzip, plain-text sitemaps).

Safety: XML with a DOCTYPE/ENTITY declaration is rejected (entity-expansion attacks), and gzip is decompressed
with a hard output cap (zip bombs). Only URLs on the same site as the sitemap's origin are accepted.
"""
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from .parser import normalize_url, site_key

MAX_SITEMAP_BYTES = 10_000_000


@dataclass
class SitemapEntry:
    loc: str
    lastmod: str | None = None
    priority: float | None = None


def _gunzip(data: bytes, limit: int) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError("sitemap too large after decompression")
    return out


def parse_sitemap(data: bytes, limit: int = MAX_SITEMAP_BYTES) -> tuple[list[SitemapEntry], list[str]]:
    """Return (page entries, child sitemap URLs). Raises ValueError for unsafe or malformed input."""
    if data[:2] == b"\x1f\x8b":
        data = _gunzip(data, limit)
    if len(data) > limit:
        raise ValueError("sitemap too large")
    head = data.lstrip()[:1]
    if head and head != b"<":  # plain-text sitemap: one URL per line
        lines = [l.strip() for l in data.decode("utf-8", errors="replace").splitlines()]
        return [SitemapEntry(l) for l in lines if l.startswith(("http://", "https://"))], []
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ValueError("sitemap contains a DOCTYPE/ENTITY declaration")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"invalid sitemap xml: {e}") from e
    local = lambda el: el.tag.rsplit("}", 1)[-1].lower()  # noqa: E731  (ignore XML namespaces)
    text = lambda el, name: next(((c.text or "").strip() for c in el if local(c) == name), "")  # noqa: E731
    pages: list[SitemapEntry] = []
    children: list[str] = []
    kind = local(root)
    for el in root:
        loc = text(el, "loc")
        if not loc:
            continue
        if kind == "sitemapindex" and local(el) == "sitemap":
            children.append(loc)
        elif kind == "urlset" and local(el) == "url":
            try:
                prio = float(text(el, "priority")) if text(el, "priority") else None
            except ValueError:
                prio = None
            pages.append(SitemapEntry(loc, text(el, "lastmod") or None, prio if prio is None or 0 <= prio <= 1 else None))
    return pages, children


def discover_sitemap_urls(
    raw_fetcher, origin: str, robots_sitemaps: list[str] | None = None, *, max_sitemaps: int = 10, max_urls: int = 5000
) -> list[SitemapEntry]:
    """Collect page URLs of ``origin`` from robots.txt-declared sitemaps (or /sitemap.xml), following indexes."""
    queue = list(robots_sitemaps or []) or [origin + "/sitemap.xml"]
    seen: set[str] = set()
    out: dict[str, SitemapEntry] = {}
    fetched = 0
    while queue and fetched < max_sitemaps and len(out) < max_urls:
        sm_url = queue.pop(0)
        nu = normalize_url(sm_url) if not sm_url.lower().endswith((".xml", ".gz", ".txt")) else sm_url
        if not nu or nu in seen or site_key(nu) != site_key(origin) or urlsplit(nu).scheme not in ("http", "https"):
            continue
        seen.add(nu)
        fetched += 1
        try:
            data = raw_fetcher(nu, timeout=10, max_bytes=5_000_000)
            pages, children = parse_sitemap(data)
        except Exception:  # missing / blocked / malformed sitemap: skip it, the crawl continues
            continue
        queue.extend(children)
        for e in pages:
            u = normalize_url(e.loc)
            if u and site_key(u) == site_key(origin) and u not in out:
                out[u] = SitemapEntry(u, e.lastmod, e.priority)
                if len(out) >= max_urls:
                    break
    return list(out.values())
