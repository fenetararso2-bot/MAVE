"""Crawler v1.0: sitemaps, robots (Crawl-delay), canonical URLs, redirects, duplicate detection,
retry/backoff, recrawl scheduling, migration v3. Offline: fake fetchers and a fake clock."""
import gzip
import os
import random
import unittest
import urllib.error
from unittest import mock

from app import db as dbmod
from app.crawler import Crawler, RobotsCache, discover_sitemap_urls, next_interval, parse_sitemap
from app.crawler import dedupe
from app.crawler.fetch import is_transient, retry_after_seconds
from app.crawler.frontier import backoff_delay
from app.crawler.scheduler import DEFAULT_INTERVAL, MAX_INTERVAL, MIN_INTERVAL
from app.db import db
from app.search.engine import search_local, stats
from tests.pgtest import PgCase, create_database, drop_database

LONG = "word " * 60  # > 80 chars of text so a page is indexable


def page(title="T", body=None, head="", links=()):
    """Page text = LONG + body (defaults to the title, so pages with different titles have different content;
    link anchors have no text so they never change the page content)."""
    anchors = " ".join(f'<a href="{h}"></a>' for h in links)
    body = title if body is None else body
    return f"<html lang='en'><head><title>{title}</title>{head}</head><body>{LONG}{body} {anchors}</body></html>"


def http_error(code, retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://s.test/", code, "err", hdrs, None)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t, self.slept = t, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class TempDB(PgCase):  # self.dsn: a private, fully migrated PostgreSQL database per test
    def setUp(self):
        super().setUp()
        self.clk = Clock()

    def crawler(self, fetch, **kw):
        kw.setdefault("delay", 0)
        kw.setdefault("respect_robots", False)
        return Crawler(fetcher=fetch, dsn=self.dsn, clock=self.clk.now, sleep=self.clk.sleep, **kw)

    def docs(self):
        with db(self.dsn) as con:
            return {r["url"]: r for r in con.execute("SELECT * FROM documents")}

    def frontier(self):
        with db(self.dsn) as con:
            return {r["url"]: r for r in con.execute("SELECT * FROM frontier")}

    @staticmethod
    def web(pages, calls=None):
        def fetch(url):
            if calls is not None:
                calls.append(url)
            if url not in pages:
                raise ValueError("404")
            v = pages[url]
            return v if isinstance(v, tuple) else (url, v)

        return fetch


# ----------------------------------------------------------------------------- sitemap
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def urlset(*entries):
    inner = "".join(f"<url><loc>{u}</loc>{extra}</url>" for u, extra in entries)
    return f'<?xml version="1.0"?><urlset xmlns="{SITEMAP_NS}">{inner}</urlset>'.encode()


class TestSitemap(unittest.TestCase):
    def test_parse_urlset(self):
        data = urlset(
            ("https://s.test/a", "<lastmod>2026-01-02</lastmod><priority>0.8</priority>"),
            ("https://s.test/b", "<priority>7</priority>"),  # out of range -> ignored
            ("https://s.test/c", ""),
        )
        pages, children = parse_sitemap(data)
        self.assertEqual([p.loc for p in pages], ["https://s.test/a", "https://s.test/b", "https://s.test/c"])
        self.assertEqual((pages[0].lastmod, pages[0].priority), ("2026-01-02", 0.8))
        self.assertIsNone(pages[1].priority)
        self.assertEqual(children, [])

    def test_parse_index_gzip_and_text(self):
        idx = f'<sitemapindex xmlns="{SITEMAP_NS}"><sitemap><loc>https://s.test/s1.xml</loc></sitemap></sitemapindex>'.encode()
        self.assertEqual(parse_sitemap(idx), ([], ["https://s.test/s1.xml"]))
        pages, _ = parse_sitemap(gzip.compress(urlset(("https://s.test/z", ""))))
        self.assertEqual([p.loc for p in pages], ["https://s.test/z"])
        pages, _ = parse_sitemap(b"https://s.test/t1\nnot a url\nhttps://s.test/t2\n")
        self.assertEqual([p.loc for p in pages], ["https://s.test/t1", "https://s.test/t2"])

    def test_rejects_entities_and_bombs_and_garbage(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
        with self.assertRaises(ValueError):
            parse_sitemap(evil)
        with self.assertRaises(ValueError):
            parse_sitemap(gzip.compress(b"<urlset>" + b"a" * 50_000), limit=1000)  # decompression cap
        with self.assertRaises(ValueError):
            parse_sitemap(b"<urlset><url>")  # malformed

    def test_discover_follows_index_and_filters_offsite(self):
        files = {
            "https://s.test/robots-map.xml": f'<sitemapindex xmlns="{SITEMAP_NS}"><sitemap><loc>https://s.test/s1.xml</loc></sitemap>'
            f"<sitemap><loc>https://evil.test/s2.xml</loc></sitemap></sitemapindex>".encode(),
            "https://s.test/s1.xml": urlset(
                ("https://s.test/a?utm_source=x", ""), ("https://www.s.test/b", ""),
                ("https://evil.test/c", ""), ("https://s.test/file.pdf", ""), ("https://s.test/a", ""),
            ),
        }
        asked = []

        def raw(url, timeout=10, max_bytes=0):
            asked.append(url)
            if url not in files:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return files[url]

        out = discover_sitemap_urls(raw, "https://s.test", ["https://s.test/robots-map.xml"])
        self.assertEqual([e.loc for e in out], ["https://s.test/a", "https://www.s.test/b"])
        self.assertNotIn("https://evil.test/s2.xml", asked)  # foreign child sitemaps are never fetched

    def test_discover_defaults_to_sitemap_xml_and_survives_errors(self):
        def raw(url, timeout=10, max_bytes=0):
            if url == "https://s.test/sitemap.xml":
                return urlset(("https://s.test/a", ""))
            raise OSError("boom")

        self.assertEqual([e.loc for e in discover_sitemap_urls(raw, "https://s.test")], ["https://s.test/a"])
        self.assertEqual(discover_sitemap_urls(lambda *a, **k: (_ for _ in ()).throw(OSError()), "https://s.test"), [])


# ----------------------------------------------------------------------------- robots
class TestRobots(unittest.TestCase):
    ROBOTS = "User-agent: *\nCrawl-delay: 5\nDisallow: /private\nSitemap: https://s.test/sm.xml\n"

    def cache(self, fn):
        return RobotsCache(lambda: "MAVEBot/0.3", fn)

    def test_rules_delay_and_sitemaps(self):
        rc = self.cache(lambda url, timeout=8, max_bytes=0: self.ROBOTS.encode())
        self.assertTrue(rc.allowed("https://s.test/public"))
        self.assertFalse(rc.allowed("https://s.test/private/x"))
        self.assertEqual(rc.crawl_delay("https://s.test/"), 5.0)
        self.assertEqual(rc.sitemaps("https://s.test"), ["https://s.test/sm.xml"])

    def test_huge_crawl_delay_is_capped(self):
        rc = self.cache(lambda url, timeout=8, max_bytes=0: b"User-agent: *\nCrawl-delay: 9999\n")
        self.assertEqual(rc.crawl_delay("https://s.test/"), 30.0)

    def test_missing_5xx_and_unreachable(self):
        def raiser(exc):
            def f(url, timeout=8, max_bytes=0):
                raise exc
            return f

        self.assertTrue(self.cache(raiser(http_error(404))).allowed("https://s.test/x"))  # no robots.txt
        self.assertFalse(self.cache(raiser(http_error(503))).allowed("https://s.test/x"))  # conservative
        self.assertFalse(self.cache(raiser(OSError("down"))).allowed("https://s.test/x"))

    def test_fetched_once_per_origin(self):
        n = []
        rc = self.cache(lambda url, timeout=8, max_bytes=0: n.append(url) or b"User-agent: *\nDisallow:\n")
        for p in ("a", "b", "c"):
            rc.allowed(f"https://s.test/{p}")
        self.assertEqual(len(n), 1)


# ----------------------------------------------------------------------------- dedupe primitives
def words(seed, n):
    r = random.Random(seed)
    return [f"w{r.randrange(5000)}" for _ in range(n)]


class TestDedupe(unittest.TestCase):
    def test_hash_ignores_case_and_whitespace(self):
        self.assertEqual(dedupe.content_hash("Hello   World\n"), dedupe.content_hash("hello world"))
        self.assertNotEqual(dedupe.content_hash("hello world"), dedupe.content_hash("hello there"))

    def test_simhash_similarity(self):
        a = words(1, 1200)
        b = list(a)
        b[600] = "changed"
        c = words(2, 1200)
        sa, sb, sc = (dedupe.simhash(" ".join(x)) for x in (a, b, c))
        self.assertEqual(dedupe.simhash(" ".join(a)), sa)  # deterministic
        self.assertLessEqual(dedupe.hamming(sa, sb), 3)
        self.assertGreater(dedupe.hamming(sa, sc), 12)

    def test_signed_roundtrip_and_band_guarantee(self):
        rnd = random.Random(7)
        for _ in range(300):
            v = rnd.getrandbits(64)
            self.assertEqual(dedupe.to_unsigned(dedupe.to_signed(v)), v)
            self.assertTrue(-(1 << 63) <= dedupe.to_signed(v) < (1 << 63))
            w = v
            for bit in rnd.sample(range(64), 3):
                w ^= 1 << bit
            self.assertTrue(set(dedupe.bands(v)) & set(dedupe.bands(w)))  # <=3 flipped bits share a band


class TestFetchHelpers(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(all(is_transient(http_error(c)) for c in (408, 429, 500, 502, 503, 504)))
        self.assertFalse(any(is_transient(http_error(c)) for c in (400, 401, 403, 404, 410)))
        self.assertTrue(is_transient(TimeoutError()))
        self.assertTrue(is_transient(ConnectionResetError()))
        self.assertTrue(is_transient(urllib.error.URLError("dns")))
        self.assertFalse(is_transient(ValueError("not html")))

    def test_retry_after_and_backoff(self):
        self.assertEqual(retry_after_seconds(http_error(429, 90)), 90.0)
        self.assertIsNone(retry_after_seconds(http_error(429)))
        self.assertIsNone(retry_after_seconds(ValueError()))
        self.assertEqual([backoff_delay(n, 30, 3600) for n in range(3)], [30, 60, 120])
        self.assertEqual(backoff_delay(20, 30, 3600), 3600)
        self.assertEqual(backoff_delay(0, 30, 3600, retry_after=500), 500)
        self.assertEqual(backoff_delay(2, 30, 3600, retry_after=5), 120)  # Retry-After never shortens the wait


# ----------------------------------------------------------------------------- crawler behaviour
class TestSitemapCrawl(TempDB):
    def test_sitemap_urls_are_crawled_by_priority(self):
        files = {
            "https://s.test/robots.txt": b"User-agent: *\nDisallow: /secret\nSitemap: https://s.test/sm.xml\n",
            "https://s.test/sm.xml": urlset(
                ("https://s.test/low", "<priority>0.1</priority>"), ("https://s.test/high", "<priority>1.0</priority>"),
                ("https://s.test/secret/x", "<priority>1.0</priority>"), ("https://other.test/x", ""),
            ),
        }

        def raw(url, timeout=10, max_bytes=0):
            if url in files:
                return files[url]
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)

        calls = []
        pages = {u: page(u) for u in ("https://s.test/", "https://s.test/low", "https://s.test/high", "https://s.test/secret/x")}
        c = self.crawler(self.web(pages, calls), respect_robots=True, raw_fetcher=raw, use_sitemaps=True, max_depth=0)
        out = c.run(["https://s.test/"])
        self.assertEqual(out["sitemap_urls"], 3)  # off-site entry dropped
        self.assertEqual(calls, ["https://s.test/", "https://s.test/high", "https://s.test/low"])  # seed, then priority
        self.assertEqual(out["blocked"], 1)  # robots.txt still applies to sitemap URLs
        self.assertEqual(set(self.docs()), {"https://s.test/", "https://s.test/high", "https://s.test/low"})

    def test_sitemaps_off_by_default(self):
        raw = mock.Mock(side_effect=AssertionError("sitemap must not be fetched"))
        c = self.crawler(self.web({"https://s.test/": page()}), raw_fetcher=raw, max_depth=0)
        self.assertEqual(c.run(["https://s.test/"])["indexed"], 1)

    def test_crawl_delay_from_robots_is_honoured(self):
        raw = lambda url, timeout=10, max_bytes=0: b"User-agent: *\nCrawl-delay: 7\n"  # noqa: E731
        pages = {"https://s.test/": page(links=["/a"]), "https://s.test/a": page()}
        c = self.crawler(self.web(pages), respect_robots=True, raw_fetcher=raw, delay=0)
        c.run(["https://s.test/"])
        self.assertEqual(self.clk.slept, [7.0])  # waited between the two hits on the same host


class TestCanonicalAndRedirects(TempDB):
    def test_canonical_url_is_indexed_instead_of_the_variant(self):
        v = "https://s.test/a?ref=1"
        pages = {v: page("A", head='<link rel="canonical" href="/a">')}
        calls = []
        self.crawler(self.web(pages, calls), max_depth=0).run([v])
        docs = self.docs()
        self.assertEqual(set(docs), {"https://s.test/a"})
        self.assertEqual(docs["https://s.test/a"]["canonical_url"], "https://s.test/a")
        self.assertEqual(self.frontier()["https://s.test/a"]["status"], "done")  # never fetched separately

    def test_cross_site_canonical_is_ignored(self):
        u = "https://s.test/a"
        pages = {u: page("A", head='<link rel="canonical" href="https://evil.test/steal">')}
        self.crawler(self.web(pages), max_depth=0).run([u])
        self.assertEqual(set(self.docs()), {u})

    def test_variant_of_an_indexed_page_does_not_overwrite_it(self):
        pages = {
            "https://s.test/a": page("Real A", links=["/a?ref=1"]),
            "https://s.test/a?ref=1": page("Variant", body="different", head='<link rel="canonical" href="/a">'),
        }
        out = self.crawler(self.web(pages), max_depth=1).run(["https://s.test/a"])
        docs = self.docs()
        self.assertEqual(set(docs), {"https://s.test/a"})
        self.assertEqual(docs["https://s.test/a"]["title"], "Real A")
        self.assertEqual(out["duplicates"], 1)
        self.assertEqual(self.frontier()["https://s.test/a?ref=1"]["status"], "duplicate")

    def test_redirect_is_indexed_under_the_final_url(self):
        pages = {"https://s.test/old": ("https://s.test/new", page("New"))}
        calls = []
        self.crawler(self.web(pages, calls), max_depth=0).run(["https://s.test/old"])
        self.assertEqual(set(self.docs()), {"https://s.test/new"})
        fr = self.frontier()
        self.assertEqual((fr["https://s.test/old"]["status"], fr["https://s.test/new"]["status"]), ("done", "done"))

    def test_offsite_redirect_is_not_indexed_but_www_is_fine(self):
        pages = {"https://s.test/x": ("https://evil.test/y", page()), "https://s.test/w": ("https://www.s.test/w", page())}
        self.crawler(self.web(pages), max_depth=0).run(["https://s.test/x", "https://s.test/w"])
        self.assertEqual(set(self.docs()), {"https://www.s.test/w"})
        self.assertEqual(self.frontier()["https://s.test/x"]["status"], "offsite")

    def test_base_href_is_used_for_relative_links(self):
        pages = {
            "https://s.test/": page(head='<base href="https://s.test/docs/">', links=["guide"]),
            "https://s.test/docs/guide": page("Guide"),
        }
        self.crawler(self.web(pages), max_depth=1).run(["https://s.test/"])
        self.assertIn("https://s.test/docs/guide", self.docs())


class TestRobotsMeta(TempDB):
    def test_noindex_is_not_indexed_but_followed_and_nofollow_is_indexed_but_not_followed(self):
        pages = {
            "https://s.test/": page("Home", links=["/noidx", "/nofol"]),
            "https://s.test/noidx": page("NoIdx", head='<meta name="robots" content="noindex, follow">', links=["/deep1"]),
            "https://s.test/nofol": page("NoFol", head='<meta name="ROBOTS" content="nofollow">', links=["/deep2"]),
            "https://s.test/deep1": page("Deep1"),
            "https://s.test/deep2": page("Deep2"),
        }
        self.crawler(self.web(pages), max_depth=3).run(["https://s.test/"])
        self.assertEqual(
            set(self.docs()), {"https://s.test/", "https://s.test/nofol", "https://s.test/deep1"}
        )

    def test_none_means_noindex_nofollow(self):
        pages = {"https://s.test/": page(head='<meta name="robots" content="none">', links=["/a"]), "https://s.test/a": page()}
        self.crawler(self.web(pages), max_depth=2).run(["https://s.test/"])
        self.assertEqual(self.docs(), {})
        self.assertNotIn("https://s.test/a", self.frontier())


class TestDuplicates(TempDB):
    def test_exact_duplicate_is_skipped(self):
        pages = {"https://s.test/1": page("One", links=["/2"]), "https://s.test/2": page("One")}
        out = self.crawler(self.web(pages), max_depth=1).run(["https://s.test/1"])
        self.assertEqual((out["indexed"], out["duplicates"]), (1, 1))
        self.assertEqual(set(self.docs()), {"https://s.test/1"})

    def test_near_duplicate_long_pages_are_skipped_but_different_ones_are_not(self):
        base = words(1, 1200)
        near = list(base)
        near[600] = "changed"
        text = lambda ws: " ".join(ws)  # noqa: E731
        pages = {
            "https://s.test/1": page("A", body=text(base), links=["/2", "/3"]),
            "https://s.test/2": page("B", body=text(near)),
            "https://s.test/3": page("C", body=text(words(2, 1200))),
        }
        out = self.crawler(self.web(pages), max_depth=1).run(["https://s.test/1"])
        self.assertEqual(set(self.docs()), {"https://s.test/1", "https://s.test/3"})
        self.assertEqual(out["duplicates"], 1)
        self.assertEqual(self.frontier()["https://s.test/2"]["status"], "duplicate")

    def test_near_duplicate_detection_can_be_disabled_and_skips_short_pages(self):
        base = words(1, 1200)
        near = list(base)
        near[600] = "changed"
        pages = {
            "https://s.test/1": page("A", body=" ".join(base), links=["/2"]),
            "https://s.test/2": page("B", body=" ".join(near)),
        }
        self.crawler(self.web(pages), max_depth=1, near_dup_distance=0).run(["https://s.test/1"])
        self.assertEqual(len(self.docs()), 2)
        short = {"https://t.test/1": page("A", body="alpha", links=["/2"]), "https://t.test/2": page("B", body="beta")}
        self.crawler(self.web(short), max_depth=1).run(["https://t.test/1"])  # ~60 tokens: exact-only
        self.assertIn("https://t.test/2", self.docs())


class TestRetry(TempDB):
    def flaky(self, errors, final_page=None):
        seq = list(errors)

        def fetch(url):
            if seq:
                raise seq.pop(0)
            return url, final_page or page("Ok")

        return fetch

    def test_backoff_then_success(self):
        c = self.crawler(self.flaky([http_error(503), http_error(503)]), max_attempts=3, backoff_base=30, max_retry_wait=600)
        out = c.run(["https://s.test/"])
        self.assertEqual((out["indexed"], out["retried"], out["failed"]), (1, 2, 0))
        self.assertEqual(self.clk.slept, [30.0, 60.0])  # exponential: 30s, 60s
        self.assertEqual(self.frontier()["https://s.test/"]["attempts"], 2)

    def test_gives_up_after_max_attempts(self):
        c = self.crawler(self.flaky([http_error(503)] * 5), max_attempts=3, backoff_base=10, max_retry_wait=600)
        out = c.run(["https://s.test/"])
        self.assertEqual((out["indexed"], out["retried"], out["failed"]), (0, 2, 1))
        row = self.frontier()["https://s.test/"]
        self.assertEqual(row["status"], "failed")
        self.assertIn("503", row["last_error"])

    def test_retry_after_header_wins_when_longer(self):
        c = self.crawler(self.flaky([http_error(429, retry_after=120)]), backoff_base=30, max_retry_wait=600)
        c.run(["https://s.test/"])
        self.assertEqual(self.clk.slept, [120.0])

    def test_timeouts_and_url_errors_are_retried(self):
        c = self.crawler(self.flaky([TimeoutError("slow"), urllib.error.URLError("reset")]), max_attempts=3, max_retry_wait=600)
        self.assertEqual(c.run(["https://s.test/"])["indexed"], 1)

    def test_long_backoff_leaves_url_pending_for_the_next_run(self):
        fetch = self.flaky([http_error(503)])
        out = self.crawler(fetch, backoff_base=7200, backoff_max=7200, max_retry_wait=60).run(["https://s.test/"])
        self.assertEqual((out["indexed"], out["retried"]), (0, 1))
        self.assertEqual(self.clk.slept, [])  # did not block the run
        row = self.frontier()["https://s.test/"]
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["next_at"], self.clk.now())
        self.clk.t += 7300
        self.assertEqual(self.crawler(fetch).run()["indexed"], 1)  # resumed later, retry now due

    def test_permanent_errors_are_not_retried(self):
        out = self.crawler(self.flaky([http_error(403)]), max_attempts=5).run(["https://s.test/"])
        self.assertEqual((out["failed"], out["retried"]), (1, 0))
        out = self.crawler(self.flaky([ValueError("not html")]), max_attempts=5).run(["https://s.test/z"])
        self.assertEqual((out["failed"], out["retried"]), (1, 0))

    def test_404_is_gone_not_failed(self):
        out = self.crawler(self.flaky([http_error(404)])).run(["https://s.test/"])
        self.assertEqual((out["failed"], out["retried"], out["removed"]), (0, 0, 0))
        self.assertEqual(self.frontier()["https://s.test/"]["status"], "gone")


class TestRecrawl(TempDB):
    DAY = 86400.0

    def test_schedule_adapts_and_dead_pages_are_removed(self):
        u = "https://s.test/"
        pages = {u: page("V1", body="alpha")}
        t0 = self.clk.t
        self.crawler(self.web(pages), max_depth=0).run([u])
        d = self.docs()[u]
        self.assertEqual((d["crawl_interval"], d["next_crawl_at"]), (DEFAULT_INTERVAL, t0 + DEFAULT_INTERVAL))

        self.clk.t = t0 + 1 * self.DAY  # not due yet
        self.assertEqual(self.crawler(self.web(pages), recrawl=True).run()["recrawl_queued"], 0)

        self.clk.t = t0 + 8 * self.DAY  # due; content unchanged -> interval doubles
        out = self.crawler(self.web(pages), recrawl=True).run()
        self.assertEqual((out["recrawl_queued"], out["unchanged"], out["indexed"]), (1, 1, 0))
        d = self.docs()[u]
        self.assertEqual(d["crawl_interval"], 2 * DEFAULT_INTERVAL)
        self.assertEqual(d["next_crawl_at"], self.clk.t + 2 * DEFAULT_INTERVAL)

        self.clk.t += 15 * self.DAY  # due again; content changed -> interval halves, index updated
        pages[u] = page("V2", body="beta")
        out = self.crawler(self.web(pages), recrawl=True).run()
        self.assertEqual((out["unchanged"], out["indexed"]), (0, 1))
        self.assertEqual(self.docs()[u]["crawl_interval"], DEFAULT_INTERVAL)
        with db(self.dsn) as con:
            self.assertEqual(len(search_local(con, "beta")), 1)
            self.assertEqual(len(search_local(con, "alpha")), 0)

        self.clk.t += 8 * self.DAY  # page disappeared -> removed from the index
        del pages[u]
        fetch = lambda url: (_ for _ in ()).throw(http_error(404))  # noqa: E731
        out = self.crawler(fetch, recrawl=True).run()
        self.assertEqual(out["removed"], 1)
        with db(self.dsn) as con:
            self.assertEqual(search_local(con, "beta"), [])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM simhash_bands").fetchone()[0], 0)

    def test_interval_bounds(self):
        self.assertEqual(next_interval(None, False), 2 * DEFAULT_INTERVAL)
        self.assertEqual(next_interval(MAX_INTERVAL, False), MAX_INTERVAL)
        self.assertEqual(next_interval(MIN_INTERVAL, True), MIN_INTERVAL)

    def test_recrawl_off_by_default(self):
        u = "https://s.test/"
        self.crawler(self.web({u: page()}), max_depth=0).run([u])
        self.clk.t += 30 * self.DAY
        self.assertEqual(self.crawler(self.web({u: page()})).run()["recrawl_queued"], 0)


class TestMigrationV3(TempDB):
    def test_upgrade_from_v2_keeps_rows_and_adds_defaults(self):
        path = create_database(migrated=False)
        self.addCleanup(drop_database, path)
        with mock.patch.object(dbmod, "MIGRATIONS", {v: dbmod.MIGRATIONS[v] for v in (1, 2)}):
            dbmod.init_db(path)
        with db(path) as con:
            self.assertEqual(dbmod.schema_version(con), 2)
            con.execute("INSERT INTO frontier(url, depth, status, added_at) VALUES('https://s.test/',0,'pending',1)")
            con.execute("INSERT INTO documents(url,domain,title,body,lang,fetched_at) VALUES('https://s.test/','s.test','t','b','en',1)")
        dbmod.init_db(path)
        with db(path) as con:
            self.assertEqual(dbmod.schema_version(con), dbmod.SCHEMA_VERSION)
            self.assertGreaterEqual(dbmod.SCHEMA_VERSION, 3)
            self.assertEqual(tuple(con.execute("SELECT priority, attempts, next_at, last_error FROM frontier").fetchone()), (0, 0, 0, None))
            self.assertEqual(tuple(con.execute("SELECT content_hash, simhash, next_crawl_at FROM documents").fetchone()), (None, None, None))
            con.execute("SELECT * FROM simhash_bands")
            # the full-text column was back-filled for the row that existed before the search migration
            self.assertEqual(con.execute("SELECT COUNT(*) FROM documents WHERE tsv @@ to_tsquery('simple', 't')").fetchone()[0], 1)

    def test_stats_report_crawler_states(self):
        pages = {"https://s.test/1": page("One", links=["/2"]), "https://s.test/2": page("One")}
        self.crawler(self.web(pages), max_depth=1).run(["https://s.test/1"])
        with db(self.dsn) as con:
            st = stats(con)
        self.assertEqual((st["documents"], st["frontier_duplicate"]), (1, 1))
        for key in ("frontier_gone", "frontier_blocked", "frontier_retrying", "documents_due_recrawl"):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main()
