"""MAVE crawler v1.0: priority frontier, robots.txt (+Crawl-delay), sitemaps, redirects, canonical URLs,
exact/near duplicate detection, retry with exponential backoff and adaptive recrawl scheduling. Stdlib only."""
import re
import time
import urllib.error
from urllib.parse import urljoin, urlsplit

from ..core.config import settings
from ..db import db
from ..search.engine import add_link_counts, refresh_vocab, upsert_document
from ..core.netguard import BlockedURL
from ..core.textutil import detect_lang
from . import frontier as fr
from .dedupe import MIN_NEAR_DUP_TOKENS, content_hash, find_near_duplicate, simhash, store_fingerprint, to_signed, token_count
from .fetch import fetch_raw, fetch_url, is_transient, retry_after_seconds
from .parser import MAX_URL_BYTES, PageParser, normalize_url, site_key
from .robots import RobotsCache
from .scheduler import DEFAULT_INTERVAL, clamp, enqueue_due, next_interval
from .sitemap import discover_sitemap_urls

SEED_PRIORITY = 100.0


class Crawler:
    def __init__(
        self,
        fetcher=fetch_url,
        dsn: str | None = None,
        max_pages: int = 100,
        max_depth: int = 2,
        delay: float = 1.0,
        stay_on_seed_domains: bool = True,
        respect_robots: bool = True,
        *,
        raw_fetcher=None,  # fetch_raw(url, timeout=, max_bytes=) -> bytes; used for robots.txt and sitemaps
        use_sitemaps: bool = False,
        max_attempts: int = 3,  # total tries for transient errors (timeouts, 429, 5xx)
        backoff_base: float = 30.0,  # retry delays: 30s, 60s, 120s ... (capped by backoff_max)
        backoff_max: float = 3600.0,
        max_retry_wait: float = 60.0,  # sleep for a pending retry only if it is due within this many seconds
        near_dup_distance: int = 3,  # SimHash Hamming distance (0 disables near-duplicate detection)
        recrawl: bool = False,  # queue documents whose recrawl time has come
        sleep=time.sleep,
        clock=time.time,
    ):
        self.fetcher = fetcher
        self.raw_fetcher = raw_fetcher or fetch_raw
        self.dsn = dsn
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.delay = delay
        self.stay = stay_on_seed_domains
        self.use_sitemaps = use_sitemaps
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_retry_wait = max_retry_wait
        self.near_dup_distance = near_dup_distance
        self.recrawl = recrawl
        self.sleep = sleep
        self.clock = clock
        self.robots = RobotsCache(lambda: settings.bot_name, self.raw_fetcher) if respect_robots else None
        self._last_hit: dict[str, float] = {}
        self._seed_sites: set[str] = set()

    # ---- politeness
    def _wait(self, url: str) -> None:
        netloc = urlsplit(url).netloc
        delay = self.delay
        if self.robots:
            delay = max(delay, self.robots.crawl_delay(url) or 0.0)
        last = self._last_hit.get(netloc)
        if last is not None and delay > 0:
            remaining = delay - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last_hit[netloc] = self.clock()

    # ---- frontier
    def add_seeds(self, seeds: list[str]) -> int:
        n = 0
        with db(self.dsn) as con:
            for s in seeds:
                u = normalize_url(s)
                if not u:
                    continue
                self._seed_sites.add(site_key(u))
                n += fr.add(con, u, 0, SEED_PRIORITY, self.clock())
        return n

    def _load_sitemaps(self, seeds: list[str]) -> int:
        origins: dict[str, None] = {}
        for s in seeds:
            u = normalize_url(s)
            if u:
                p = urlsplit(u)
                origins[f"{p.scheme}://{p.netloc}"] = None
        added = 0
        for origin in origins:
            maps = self.robots.sitemaps(origin) if self.robots else []
            entries = discover_sitemap_urls(self.raw_fetcher, origin, maps)
            with db(self.dsn) as con:
                for e in entries:
                    u = normalize_url(e.loc)
                    if not u or (self.stay and site_key(u) not in self._seed_sites):
                        continue
                    prio = 60 + 20 * (e.priority if e.priority is not None else 0.5)
                    added += fr.add(con, u, 1, prio, self.clock())
        return added

    # ---- main loop
    def run(self, seeds: list[str] | None = None) -> dict:
        st = dict(indexed=0, failed=0, blocked=0, duplicates=0, unchanged=0, retried=0, removed=0,
                  sitemap_urls=0, recrawl_queued=0)
        if seeds:
            self.add_seeds(seeds)
        else:
            with db(self.dsn) as con:
                for r in con.execute("SELECT url FROM frontier"):
                    self._seed_sites.add(site_key(r["url"]))
        with db(self.dsn) as con:  # recover from an interrupted run
            fr.recover_interrupted(con)
            if self.recrawl:
                st["recrawl_queued"] = len(enqueue_due(con, self.clock()))
        if seeds and self.use_sitemaps:
            st["sitemap_urls"] = self._load_sitemaps(seeds)
        budget, fetched, idle = self.max_pages * 10 + 50, 0, 0
        while st["indexed"] < self.max_pages and fetched < budget:
            now = self.clock()
            with db(self.dsn) as con:
                row = fr.claim_next(con, now)
                wait = None if row else fr.seconds_until_ready(con, now)
            if row is None:
                idle += 1
                if wait is not None and wait <= self.max_retry_wait and idle < 10_000:
                    self.sleep(wait)  # a retry is due soon: wait for it instead of ending the run
                    continue
                break
            fetched += 1
            self._process(row["url"], row["depth"], row["attempts"], st)
        if st["indexed"] or st["removed"]:  # keep the spelling vocabulary in step with the index
            with db(self.dsn) as con:
                refresh_vocab(con)
        return st

    def _mark(self, url: str, status: str, error: str | None = None) -> None:
        with db(self.dsn) as con:
            fr.mark(con, url, status, error)

    def _process(self, url: str, depth: int, attempts: int, st: dict) -> None:
        if self.robots and not self.robots.allowed(url):
            st["blocked"] += 1
            self._mark(url, "blocked")
            return
        self._wait(url)
        try:
            final_url, html = self.fetcher(url)
        except BlockedURL:  # SSRF policy: never count/retry internal targets as ordinary failures
            st["blocked"] += 1
            self._mark(url, "blocked")
            return
        except Exception as exc:
            self._fetch_failed(url, attempts, exc, st)
            return
        parser = PageParser()
        try:
            parser.feed(html)
        except Exception as exc:
            st["failed"] += 1
            self._mark(url, "failed", f"parse error: {exc!r}")
            return

        final = normalize_url(final_url) or final_url  # redirect target
        if len(final.encode("utf-8", "ignore")) > MAX_URL_BYTES:  # e.g. a redirect to an absurdly long URL
            st["failed"] += 1
            self._mark(url, "failed", "redirect target too long")
            return
        if final != url and self.stay and site_key(final) not in self._seed_sites:
            self._mark(url, "offsite")  # redirected away from the crawled sites
            return
        base = urljoin(final_url, parser.base_href) if parser.base_href else final_url
        doc_url = final
        if parser.canonical:  # honour rel=canonical, but only within the same site (no cross-domain hijacking)
            cu = normalize_url(urljoin(base, parser.canonical))
            if cu and cu != final and site_key(cu) == site_key(final):
                doc_url = cu

        text = parser.text
        body = (parser.meta_desc + " " + text).strip()
        title = re.sub(r"\s+", " ", parser.title).strip() or doc_url
        links = [nu for nu in (normalize_url(urljoin(base, h)) for h in parser.links) if nu]
        now = self.clock()
        outcome = None
        with db(self.dsn) as con:
            if not parser.noindex and len(text) >= 80:
                outcome = self._index(con, doc_url, final, title, body, text, parser.html_lang, st, now)
            add_link_counts(con, links)
            if depth < self.max_depth and not parser.nofollow:
                for nu in dict.fromkeys(links):
                    if self.stay and site_key(nu) not in self._seed_sites:
                        continue
                    fr.add(con, nu, depth + 1, 10.0 / (depth + 2), now)
            fr.mark(con, url, "duplicate" if outcome == "duplicate" else "done")
            for alias in {final, doc_url} - {url}:  # redirect target / canonical URL: do not fetch them again
                fr.add_done(con, alias, depth, now)

    def _index(self, con, doc_url, final, title, body, text, html_lang, st, now) -> str:
        row = con.execute("SELECT id, content_hash, crawl_interval FROM documents WHERE url=?", (doc_url,)).fetchone()
        if doc_url != final and row is not None:  # alias of a page that is already indexed: keep the canonical copy
            st["duplicates"] += 1
            return "duplicate"
        digest = content_hash(body)
        if row is not None and row["content_hash"] == digest:  # unchanged since the last visit
            interval = next_interval(row["crawl_interval"], changed=False)
            con.execute(
                "UPDATE documents SET fetched_at=?, crawl_interval=?, next_crawl_at=? WHERE id=?",
                (now, interval, now + interval, row["id"]),
            )
            st["unchanged"] += 1
            return "unchanged"
        if con.execute("SELECT 1 FROM documents WHERE content_hash=? AND url<>? LIMIT 1", (digest, doc_url)).fetchone():
            st["duplicates"] += 1
            return "duplicate"
        sh = None
        if self.near_dup_distance > 0 and token_count(body) >= MIN_NEAR_DUP_TOKENS:
            sh = simhash(body)
            if find_near_duplicate(con, sh, doc_url, self.near_dup_distance):
                st["duplicates"] += 1
                return "duplicate"
        upsert_document(con, doc_url, title, body, detect_lang(text, html_lang))
        interval = next_interval(row["crawl_interval"], changed=True) if row is not None else clamp(DEFAULT_INTERVAL)
        con.execute(
            """UPDATE documents SET canonical_url=?, content_hash=?, simhash=?, crawl_interval=?, next_crawl_at=?
               WHERE url=?""",
            (doc_url, digest, None if sh is None else to_signed(sh), interval, now + interval, doc_url),
        )
        store_fingerprint(con, doc_url, sh)
        st["indexed"] += 1
        return "indexed"

    def _fetch_failed(self, url: str, attempts: int, exc: Exception, st: dict) -> None:
        err = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, urllib.error.HTTPError) and exc.code in (404, 410):
            with db(self.dsn) as con:  # the page is gone: drop it from the index
                st["removed"] += con.execute("DELETE FROM documents WHERE url=?", (url,)).rowcount
                fr.mark(con, url, "gone", err)
            return
        if is_transient(exc) and attempts + 1 < self.max_attempts:
            delay = fr.backoff_delay(attempts, self.backoff_base, self.backoff_max, retry_after_seconds(exc))
            with db(self.dsn) as con:
                fr.reschedule(con, url, attempts + 1, self.clock() + delay, err)
            st["retried"] += 1
            return
        st["failed"] += 1
        self._mark(url, "failed", err)
