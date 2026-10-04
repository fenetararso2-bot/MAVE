import os
import time
import unittest
from unittest import mock

from app.core import security
from app.crawler import Crawler, PageParser, normalize_url
from app.db import db
from app.search.engine import add_link_counts, refresh_vocab, search_local, stats, upsert_document
from app.search.ranking import diversify, rrf_fuse
from app.search.suggest import log_query, spell_correct, suggest, trending
from app.core.textutil import detect_lang, fts_query, make_snippet
from tests.pgtest import PgCase, create_database, drop_database


def lorem(topic: str, n: int = 30) -> str:
    return (f"{topic} is discussed here in detail. " * n)[:2000]


TempDB = PgCase  # every test method gets its own migrated PostgreSQL database (self.dsn)


class TestRetrieval(TempDB):
    def test_bm25_and_title_boost(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Kotlin coroutines guide", lorem("coroutines in kotlin"))
            upsert_document(con, "https://b.com/1", "Cooking pasta", lorem("pasta and kotlin once"))
            upsert_document(con, "https://c.com/1", "Gardening", lorem("tomatoes"))
            res = search_local(con, "kotlin coroutines")
        self.assertEqual(res[0]["url"], "https://a.com/1")
        self.assertNotIn("https://c.com/1", [r["url"] for r in res])
        self.assertTrue(res[0]["snippet"])

    def test_prefix_and_or_fallback(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Oromo language", lorem("afaan oromoo"))
            res = search_local(con, "afaan orom")  # prefix on last term
            self.assertEqual(len(res), 1)
            res = search_local(con, "oromoo zzzunknown")  # AND fails -> OR fallback
            self.assertEqual(len(res), 1)

    def test_oromoo_stem_search(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.om/1", "Barattoota Oromiyaa", lorem("barattoota yunivarsiitii"))
            upsert_document(con, "https://b.com/1", "Gardening", lorem("tomatoes"))
            res = search_local(con, "barataa", query_lang="om")  # singular finds the plural form
            self.assertEqual([r["url"] for r in res], ["https://a.om/1"])
            self.assertEqual(search_local(con, "barataa"), [])  # without the Oromoo hint: exact prefix only (as before)

    def test_update_reindexes(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Old title", lorem("alpha"))
            self.assertEqual(len(search_local(con, "alpha")), 1)
            upsert_document(con, "https://a.com/1", "New title", lorem("beta"))
            self.assertEqual(len(search_local(con, "alpha")), 0)
            self.assertEqual(len(search_local(con, "beta")), 1)

    def test_link_popularity_breaks_ties(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/x", "Search engines", lorem("search engines"))
            upsert_document(con, "https://b.com/y", "Search engines", lorem("search engines"))
            add_link_counts(con, ["https://b.com/y"] * 1)
            add_link_counts(con, ["https://b.com/y"])
            res = search_local(con, "search engines")
        self.assertEqual(res[0]["url"], "https://b.com/y")

    def test_query_syntax_is_safe(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Hello", lorem("hello world"))
            for evil in ['hello" OR "x', "hello* NOT", "(((", '"', "NEAR(a b)", "title:hello"]:
                search_local(con, evil)  # must not raise
        self.assertEqual(fts_query('foo" OR "bar'), "'foo' & 'bar':*")  # operators are stripped, never executed

    def test_domain_diversity(self):
        items = [{"url": f"https://same.com/{i}", "domain": "same.com"} for i in range(6)]
        items.append({"url": "https://other.com/1", "domain": "other.com"})
        out = diversify(items, per_domain=2, window=3)
        # first 3 slots: at most 2 from same.com, the other domain is promoted into the window
        self.assertEqual(sum(1 for i in out[:3] if i["domain"] == "same.com"), 2)
        self.assertIn("https://other.com/1", [i["url"] for i in out[:3]])
        self.assertEqual(len(out), 7)
        # when there is nothing else to show, the window is back-filled instead of left short
        only = diversify(items[:6], per_domain=2, window=4)
        self.assertEqual(len(only), 6)


class TestSuggest(TempDB):
    def test_spell_correct(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Technology news", lorem("technology"))
            refresh_vocab(con)
            self.assertEqual(spell_correct(con, "tecnology news"), "technology news")
            self.assertIsNone(spell_correct(con, "technology news"))

    def test_suggest_and_trending(self):
        with db(self.dsn) as con:
            for q in ["kotlin tutorial"] * 3 + ["kotlin flow", "kitchen"]:
                log_query(con, q)
            s = suggest(con, "kot")
            self.assertEqual(s[0], "kotlin tutorial")
            self.assertNotIn("kitchen", s)
            self.assertEqual(trending(con)[0], "kotlin tutorial")
            self.assertEqual(suggest(con, "100%"), [])  # LIKE wildcard is escaped


class TestCrawler(TempDB):
    def make_web(self):
        long = "word " * 60
        pages = {
            "https://site.test/": f'<html lang="en"><title>Home</title><body>{long}<a href="/a">a</a> <a href="/b?utm_source=x">b</a> <a href="https://evil.test/">out</a></body></html>',
            "https://site.test/a": f'<html><title>Page A</title><body>{long} alpha <a href="/b">b</a><script>secret()</script></body></html>',
            "https://site.test/b": f'<html><title>Page B</title><body>{long} beta <a href="/c">c</a></body></html>',
            "https://site.test/c": f"<html><title>Page C</title><body>{long} gamma</body></html>",
            "https://evil.test/": f"<html><title>Evil</title><body>{long}</body></html>",
        }

        def fetch(url):
            if url not in pages:
                raise ValueError("404")
            return url, pages[url]

        return fetch

    def test_crawl_depth_domains_and_links(self):
        c = Crawler(fetcher=self.make_web(), dsn=self.dsn, max_depth=1, delay=0, respect_robots=False)
        out = c.run(["https://site.test/"])
        with db(self.dsn) as con:
            urls = {r["url"] for r in con.execute("SELECT url FROM documents")}
            counts = {r["url"]: r["n"] for r in con.execute("SELECT url, n FROM link_counts")}
            st = stats(con)
        self.assertEqual(urls, {"https://site.test/", "https://site.test/a", "https://site.test/b"})
        self.assertEqual(out["indexed"], 3)
        self.assertEqual(counts["https://site.test/b"], 2)  # linked from / and /a (utm param stripped)
        self.assertEqual(st["frontier_failed"], 0)

    def test_script_text_not_indexed_and_resume(self):
        c = Crawler(fetcher=self.make_web(), dsn=self.dsn, max_depth=3, max_pages=2, delay=0, respect_robots=False)
        c.run(["https://site.test/"])
        with db(self.dsn) as con:
            self.assertEqual(len(search_local(con, "secret")), 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
        c2 = Crawler(fetcher=self.make_web(), dsn=self.dsn, max_depth=3, max_pages=10, delay=0, respect_robots=False)
        c2.run()  # resumes from the stored frontier
        with db(self.dsn) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 4)

    def test_failed_fetch_is_recorded(self):
        c = Crawler(fetcher=self.make_web(), dsn=self.dsn, delay=0, respect_robots=False)
        out = c.run(["https://site.test/missing"])
        self.assertEqual(out["failed"], 1)

    def test_helpers(self):
        self.assertEqual(normalize_url("HTTP://Example.com:80/a?utm_x=1&k=v#frag"), "http://example.com/a?k=v")
        self.assertIsNone(normalize_url("mailto:a@b.c"))
        self.assertIsNone(normalize_url("https://x.com/file.pdf"))
        p = PageParser()
        p.feed("<html lang='om'><head><title>T</title><meta name='description' content='D'></head><body>Hi <style>x{}</style>there</body></html>")
        self.assertEqual((p.title, p.meta_desc, p.html_lang, p.text), ("T", "D", "om", "Hi there"))


class TestMisc(unittest.TestCase):
    def test_jwt(self):
        t = security.make_token(7, "a@b.co", secret="s")
        self.assertEqual(security.verify_token(t, secret="s")["sub"], 7)
        self.assertIsNone(security.verify_token(t, secret="other"))
        h, p, sig = t.split(".")
        self.assertIsNone(security.verify_token(f"{h}.{p}x.{sig}", secret="s"))
        self.assertIsNone(security.verify_token(security.make_token(1, "a@b.co", ttl=-5, secret="s"), secret="s"))
        self.assertIsNone(security.verify_token("garbage", secret="s"))

    def test_password(self):
        salt, h = security.hash_password("correct horse")
        self.assertTrue(security.check_password("correct horse", salt, h))
        self.assertFalse(security.check_password("wrong", salt, h))

    def test_rrf(self):
        a = [{"url": "https://x.com/1", "title": "x", "snippet": "s", "source": "mave"},
             {"url": "https://y.com/2", "title": "y", "snippet": "s", "source": "mave"}]
        b = [{"url": "https://www.y.com/2/", "title": "y", "snippet": "a longer snippet", "source": "web", "thumbnail": "t"},
             {"url": "https://z.com/3", "title": "z", "snippet": "s", "source": "web"}]
        out = rrf_fuse([a, b])
        self.assertEqual(out[0]["url"].rstrip("/").split("//")[-1].replace("www.", ""), "y.com/2")
        self.assertEqual(out[0]["source"], "mave+web")
        self.assertEqual(out[0]["thumbnail"], "t")
        self.assertEqual(len(out), 3)

    def test_language_and_snippet(self):
        om = "Afaan Oromoo afaan Itoophiyaa keessatti baay'ee dubbatamu dha fi kan biroo irra baay'ee fayyadamu ture"
        en = "The search engine is what people use to find the pages that they need from the web and it is fast"
        self.assertEqual(detect_lang(om), "om")
        self.assertEqual(detect_lang(en), "en")
        self.assertEqual(detect_lang("x", "om-ET"), "om")
        s = make_snippet("a " * 200 + "needle in haystack " + "b " * 200, ["needle"])
        self.assertIn("needle", s)


# ---- API tests (run only when FastAPI is installed, e.g. in CI / Docker)
try:
    from fastapi.testclient import TestClient
    HAVE_API = True
except Exception:  # pragma: no cover
    HAVE_API = False


@unittest.skipUnless(HAVE_API, "fastapi not installed")
class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = create_database()
        cls.addClassCleanup(drop_database, cls.dsn)
        os.environ["MAVE_DATABASE_URL"] = cls.dsn
        os.environ["MAVE_ADMIN_KEY"] = "adminkey"
        os.environ["MAVE_RATE_LIMIT"] = "100000"
        os.environ["MAVE_AUTH_RATE_LIMIT"] = "100000"
        for k in ("BRAVE_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "MAVE_AI_PROVIDER"):
            os.environ.pop(k, None)  # tests must never reach a real provider, even if .env was exported
        from app.main import app
        cls.c = TestClient(app)

    def test_flow(self):
        c = self.c
        self.assertTrue(c.get("/health").json()["ok"])
        r = c.post("/auth/register", json={"email": "u@example.com", "password": "password123"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post("/auth/register", json={"email": "u@example.com", "password": "password123"}).status_code, 409)
        self.assertEqual(c.post("/auth/login", json={"email": "u@example.com", "password": "nopenope1"}).status_code, 401)
        h = {"Authorization": "Bearer " + r.json()["token"]}
        self.assertEqual(c.get("/me/history").status_code, 401)
        c.post("/me/history", json={"query": "kotlin"}, headers=h)
        self.assertEqual(c.get("/me/history", headers=h).json()["items"], ["kotlin"])
        c.post("/me/saved", json={"title": "T", "url": "https://a.com"}, headers=h)
        self.assertEqual(len(c.get("/me/saved", headers=h).json()["items"]), 1)
        c.delete("/me/saved", params={"url": "https://a.com"}, headers=h)
        self.assertEqual(c.get("/me/saved", headers=h).json()["items"], [])
        res = c.get("/search", params={"q": "hello", "type": "web"}).json()
        self.assertTrue(res["results"])
        # v0.10.5: advisory category hint + guessed query language (additive fields; never change the results)
        self.assertIn("intent", res)
        self.assertIsNone(res["intent"])  # "hello" has no category cue
        news = c.get("/search", params={"q": "latest news of football", "type": "web"}).json()
        self.assertEqual((news["intent"], news["query_lang"]), ("news", "en"))
        self.assertEqual(c.get("/search", params={"q": "oduu har'a fi Oromiyaa", "type": "web"}).json()["query_lang"], "om")
        self.assertEqual(c.get("/search", params={"q": "x", "type": "bad"}).status_code, 400)
        ai = c.post("/ai/answer", json={"query": "hello", "lang": "om"}).json()
        self.assertTrue({"answer", "sources", "citations", "grounded", "insufficient"} <= set(ai))
        self.assertIsInstance(ai["citations"], list)
        self.assertEqual(c.get("/admin/stats").status_code, 403)
        self.assertIn("documents", c.get("/admin/stats", headers={"X-Admin-Key": "adminkey"}).json())
        self.assertIn("suggestions", c.get("/suggest", params={"q": "he"}).json())
        self.assertTrue(c.get("/trending").json()["trending"])


    def test_api_v1_account_lifecycle(self):
        c = self.c
        sent = []

        def fake_send(to, subject, body):
            sent.append((to, subject, body))
            return True

        def code(mail):  # the one-time code printed on its own line after "code in the app:"
            return mail[2].split("code in the app:\n")[1].split("\n")[0]

        with mock.patch("app.mailer.send", side_effect=fake_send):
            # --- versioned paths; the un-prefixed legacy paths keep working
            self.assertTrue(c.get("/api/v1/health").json()["ok"])
            r = c.post("/api/v1/auth/register", json={"email": "life@example.com", "password": "correct-horse-1"})
            self.assertEqual(r.status_code, 200)
            pair = r.json()
            self.assertEqual(set(pair), {"token", "refresh_token", "expires_in", "email", "email_verified"})
            self.assertFalse(pair["email_verified"])
            self.assertEqual(len(sent), 1)  # verification mail (background task)
            h = {"Authorization": "Bearer " + pair["token"]}

            # --- headers set by the middleware; auth responses must not be cached
            me = c.get("/api/v1/me", headers=h)
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.headers["cache-control"], "no-store")
            self.assertEqual(me.headers["x-content-type-options"], "nosniff")
            self.assertFalse(me.json()["email_verified"])

            # --- verify e-mail
            self.assertEqual(c.post("/api/v1/auth/verify-email", json={"token": code(sent[0])}).status_code, 200)
            self.assertTrue(c.get("/api/v1/me", headers=h).json()["email_verified"])
            self.assertEqual(c.post("/api/v1/auth/verify-email", json={"token": code(sent[0])}).status_code, 400)

            # --- preferences
            self.assertEqual(c.put("/api/v1/me/preferences", json={"lang": "om"}, headers=h).json(), {"lang": "om", "theme": None})
            self.assertEqual(c.put("/api/v1/me/preferences", json={"lang": "zz"}, headers=h).status_code, 400)

            # --- refresh rotation + replay detection ends the whole session
            r2 = c.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
            self.assertEqual(r2.status_code, 200)
            pair2 = r2.json()
            self.assertNotEqual(pair2["refresh_token"], pair["refresh_token"])
            self.assertEqual(c.post("/api/v1/auth/refresh", json={"refresh_token": "short"}).status_code, 422)
            replay = c.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
            self.assertEqual(replay.status_code, 401)
            self.assertEqual(replay.headers["www-authenticate"], "Bearer")
            self.assertEqual(c.get("/api/v1/me", headers={"Authorization": "Bearer " + pair2["token"]}).status_code, 401)

            # --- logout with only the refresh token (access token already expired on the device)
            p3 = c.post("/api/v1/auth/login", json={"email": "life@example.com", "password": "correct-horse-1"}).json()
            h3 = {"Authorization": "Bearer " + p3["token"]}
            self.assertEqual(c.get("/api/v1/me", headers=h3).status_code, 200)
            self.assertTrue(c.post("/api/v1/auth/logout", json={"refresh_token": p3["refresh_token"]}).json()["ok"])
            self.assertEqual(c.get("/api/v1/me", headers=h3).status_code, 401)  # immediate, not at token expiry

            # --- forgot / reset password: identical answer for unknown accounts, old sessions die
            sent.clear()
            p4 = c.post("/api/v1/auth/login", json={"email": "life@example.com", "password": "correct-horse-1"}).json()
            self.assertEqual(c.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"}).json(), {"ok": True})
            self.assertEqual(sent, [])
            self.assertEqual(c.post("/api/v1/auth/forgot-password", json={"email": "life@example.com"}).json(), {"ok": True})
            self.assertEqual(len(sent), 1)
            rt = code(sent[0])
            self.assertEqual(c.post("/api/v1/auth/reset-password", json={"token": rt, "password": "short"}).status_code, 422)
            self.assertEqual(c.post("/api/v1/auth/reset-password", json={"token": rt, "password": "brand-new-pass-9"}).status_code, 200)
            self.assertEqual(c.post("/api/v1/auth/reset-password", json={"token": rt, "password": "another-pass-77"}).status_code, 400)
            self.assertEqual(c.get("/api/v1/me", headers={"Authorization": "Bearer " + p4["token"]}).status_code, 401)
            self.assertEqual(c.post("/auth/login", json={"email": "life@example.com", "password": "correct-horse-1"}).status_code, 401)
            p5 = c.post("/auth/login", json={"email": "life@example.com", "password": "brand-new-pass-9"}).json()  # legacy path
            h5 = {"Authorization": "Bearer " + p5["token"]}

            # --- data + deletion of the account
            c.post("/api/v1/me/saved", json={"title": "T", "url": "https://b.com"}, headers=h5)
            self.assertEqual(c.post("/api/v1/me/delete", json={"password": "wrong-password"}, headers=h5).status_code, 401)
            self.assertEqual(c.post("/api/v1/me/delete", json={"password": "brand-new-pass-9"}, headers=h5).status_code, 200)
            self.assertEqual(c.get("/api/v1/me", headers=h5).status_code, 401)
            self.assertEqual(c.post("/api/v1/auth/login", json={"email": "life@example.com", "password": "brand-new-pass-9"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
