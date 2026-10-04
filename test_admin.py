"""Admin dashboard data layer, metrics, structured logging, migration 4 and the static web app (v0.8).

Everything except TestAdminAPI runs without FastAPI. TestAdminAPI is skipped unless fastapi/httpx are installed
(``pip install -r requirements.txt``), exactly like TestAPI in test_engine.py.
"""
import json
import logging
import os
import re
import time
import unittest
from pathlib import Path
from unittest import mock

from app import admin, auth
from app.core import logsetup, metrics
from app.db import MIGRATIONS, SCHEMA_VERSION, db, schema_version
from app.search.engine import upsert_document
from app.search.suggest import log_query
from tests.pgtest import PgCase, create_database, drop_database

try:
    from fastapi.testclient import TestClient

    HAVE_API = True
except ImportError:  # pragma: no cover
    HAVE_API = False

WEB = Path(__file__).resolve().parent.parent / "web"


TempDB = PgCase  # every test method gets its own migrated PostgreSQL database (self.dsn)


class TestMigration4(TempDB):
    def test_schema_version_and_tables(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 4)
        self.assertIn(4, MIGRATIONS)
        with db(self.dsn) as con:
            self.assertEqual(schema_version(con), SCHEMA_VERSION)
            tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        self.assertTrue({"seeds", "audit_log"} <= tables)


class TestSeedsAndAudit(TempDB):
    def test_add_normalizes_and_deduplicates(self):
        with db(self.dsn) as con:
            a = admin.add_seed(con, "HTTPS://Example.ORG:443/?utm_source=x")
            b = admin.add_seed(con, "https://example.org/")
            self.assertEqual(a, b)
            self.assertEqual(len(admin.list_seeds(con)), 1)
            self.assertEqual(admin.enabled_seed_urls(con), [a])

    def test_rejects_non_http_and_junk(self):
        with db(self.dsn) as con:
            for bad in ("javascript:alert(1)", "ftp://x.org/", "file:///etc/passwd", "", "not a url"):
                with self.assertRaises(admin.AdminError, msg=bad) as cm:
                    admin.add_seed(con, bad)
                self.assertEqual(cm.exception.status, 400)

    def test_disable_enable_remove(self):
        with db(self.dsn) as con:
            url = admin.add_seed(con, "https://a.org/")
            admin.set_seed_enabled(con, url, False)
            self.assertEqual(admin.enabled_seed_urls(con), [])
            admin.set_seed_enabled(con, url, True)
            self.assertEqual(admin.enabled_seed_urls(con), [url])
            admin.remove_seed(con, url)
            self.assertEqual(admin.list_seeds(con), [])
            for fn in (lambda: admin.remove_seed(con, url), lambda: admin.set_seed_enabled(con, url, True)):
                with self.assertRaises(admin.AdminError) as cm:
                    fn()
                self.assertEqual(cm.exception.status, 404)

    def test_seed_cap(self):
        with db(self.dsn) as con:
            with mock.patch.object(admin, "MAX_SEEDS", 2):
                admin.add_seed(con, "https://a.org/")
                admin.add_seed(con, "https://b.org/")
                with self.assertRaises(admin.AdminError) as cm:
                    admin.add_seed(con, "https://c.org/")
        self.assertEqual(cm.exception.status, 409)

    def test_audit_is_newest_first_and_truncates(self):
        with db(self.dsn) as con:
            admin.audit(con, "seed.add", "one")
            admin.audit(con, "crawl.start", "x" * 2000)
            rows = admin.list_audit(con)
        self.assertEqual([r["action"] for r in rows], ["crawl.start", "seed.add"])
        self.assertEqual(len(rows[0]["detail"]), 500)


class TestCrawlerAndIndexViews(TempDB):
    def test_crawl_errors_only_problem_rows(self):
        with db(self.dsn) as con:
            now = time.time()
            for url, status, attempts, err in [
                ("https://ok.org/", "done", 0, None),
                ("https://bad.org/", "failed", 1, "HTTP 403"),
                ("https://robots.org/", "blocked", 0, "robots.txt"),
                ("https://gone.org/", "gone", 0, "404"),
                ("https://retry.org/", "pending", 2, "timeout"),
                ("https://fresh.org/", "pending", 0, None),
            ]:
                con.execute(
                    "INSERT INTO frontier(url, depth, status, added_at, attempts, last_error) VALUES(?,?,?,?,?,?)",
                    (url, 0, status, now, attempts, err),
                )
            urls = {r["url"] for r in admin.crawl_errors(con)}
        self.assertEqual(urls, {"https://bad.org/", "https://robots.org/", "https://gone.org/", "https://retry.org/"})

    def test_top_domains_and_languages(self):
        with db(self.dsn) as con:
            for i in range(3):
                upsert_document(con, f"https://a.org/{i}", "t", "body text", "en")
            upsert_document(con, "https://b.org/1", "t", "body text", "om")
            upsert_document(con, "https://c.org/1", "t", "body text", None)
            doms = admin.top_domains(con)
            langs = {r["lang"]: r["documents"] for r in admin.language_breakdown(con)}
        self.assertEqual(doms[0], {"domain": "a.org", "documents": 3})
        self.assertEqual(langs, {"en": 3, "om": 1, "unknown": 1})


class TestAnalytics(TempDB):
    def test_per_day_and_top_queries(self):
        with db(self.dsn) as con:
            for q in ["kotlin", "kotlin", "kotlin", "oromoo", "AI"]:
                log_query(con, q)
            con.execute("INSERT INTO query_log(query, ts) VALUES('ancient', ?)", (time.time() - 40 * 86400,))
            a = admin.query_analytics(con, days=7)
        self.assertEqual(a["total"], 5)  # the 40-day-old query is outside the window
        self.assertEqual(a["top_queries"][0], {"query": "kotlin", "n": 3})
        self.assertEqual(sum(d["n"] for d in a["per_day"]), 5)

    def test_days_clamped(self):
        with db(self.dsn) as con:
            self.assertEqual(admin.query_analytics(con, days=10_000)["days"], 90)
            self.assertEqual(admin.query_analytics(con, days=-5)["days"], 1)


class TestUsers(TempDB):
    def _register(self, con, email):
        pair, _ = auth.register(con, email, "password123")
        return pair

    def test_list_search_and_pagination(self):
        with db(self.dsn) as con:
            for e in ("anna@example.com", "bob@example.com", "carl@sample.org"):
                self._register(con, e)
            everyone = admin.list_users(con)
            only_example = admin.list_users(con, q="EXAMPLE")
            page = admin.list_users(con, limit=1, offset=1)
            wildcard = admin.list_users(con, q="%")
        self.assertEqual(everyone["total"], 3)
        self.assertEqual({u["email"] for u in only_example["items"]}, {"anna@example.com", "bob@example.com"})
        self.assertEqual((page["total"], len(page["items"])), (3, 1))
        self.assertEqual(wildcard["total"], 0)  # LIKE wildcards are escaped, not interpreted
        for u in everyone["items"]:
            self.assertEqual(u["active_sessions"], 1)
            self.assertNotIn("pw_hash", u)
            self.assertNotIn("salt", u)

    def test_revoke_sessions_ends_access_immediately(self):
        with db(self.dsn) as con:
            pair = self._register(con, "dana@example.com")
            uid = con.execute("SELECT id FROM users WHERE email=?", ("dana@example.com",)).fetchone()["id"]
            sid = con.execute("SELECT id FROM sessions WHERE user_id=?", (uid,)).fetchone()["id"]
            self.assertTrue(auth.session_active(con, sid, uid))
            self.assertEqual(admin.revoke_user_sessions(con, uid), 1)
            self.assertFalse(auth.session_active(con, sid, uid))
            self.assertEqual(admin.revoke_user_sessions(con, uid), 0)  # idempotent
            self.assertEqual(admin.list_users(con)["items"][0]["active_sessions"], 0)
            with self.assertRaises(admin.AdminError) as cm:
                admin.revoke_user_sessions(con, 9999)
        self.assertEqual(cm.exception.status, 404)
        self.assertTrue(pair["token"])


class TestDisableUser(TempDB):
    def test_disabled_account_is_locked_out_and_can_be_restored(self):
        with db(self.dsn) as con:
            pair, _ = auth.register(con, "lock@example.com", "password123")
            uid = con.execute("SELECT id FROM users WHERE email=?", ("lock@example.com",)).fetchone()["id"]
            sid = con.execute("SELECT id FROM sessions WHERE user_id=?", (uid,)).fetchone()["id"]
            self.assertEqual(admin.set_user_disabled(con, uid, True), 1)  # the open session is revoked
            self.assertFalse(auth.session_active(con, sid, uid))
            with self.assertRaises(auth.AuthError) as cm:
                auth.refresh(con, pair["refresh_token"])
            self.assertEqual(cm.exception.status, 401)
            with self.assertRaises(auth.AuthError) as cm:
                auth.login(con, "lock@example.com", "password123")
            self.assertEqual((cm.exception.status, cm.exception.detail), (403, "Account disabled"))
            with self.assertRaises(auth.AuthError) as cm:  # wrong password still looks like any wrong password
                auth.login(con, "lock@example.com", "not-the-password")
            self.assertEqual(cm.exception.status, 401)
            self.assertTrue(admin.list_users(con)["items"][0]["disabled"])
            self.assertEqual(admin.set_user_disabled(con, uid, False), 0)
            self.assertTrue(auth.login(con, "lock@example.com", "password123")["token"])
            self.assertFalse(admin.list_users(con)["items"][0]["disabled"])
            with self.assertRaises(admin.AdminError) as cm:
                admin.set_user_disabled(con, 9999, True)
            self.assertEqual(cm.exception.status, 404)


class TestUserRole(TempDB):
    def _two_users(self, con):
        auth.register(con, "r1@example.com", "password123")
        auth.register(con, "r2@example.com", "password123")
        ids = {r["email"]: r["id"] for r in con.execute("SELECT id, email FROM users").fetchall()}
        return ids["r1@example.com"], ids["r2@example.com"]

    def test_migration_7_adds_role_defaulting_to_user(self):
        self.assertIn(7, MIGRATIONS)
        with db(self.dsn) as con:
            self.assertGreaterEqual(schema_version(con), 7)
            self.assertEqual({u["role"] for u in admin.list_users(con)["items"]}, set())  # empty database
            a, b = self._two_users(con)
            self.assertEqual({u["role"] for u in admin.list_users(con)["items"]}, {"user"})

    def test_database_itself_rejects_an_unknown_role(self):
        with db(self.dsn) as con:
            a, _ = self._two_users(con)
        with self.assertRaises(Exception):
            with db(self.dsn) as con:
                con.execute("UPDATE users SET role='root' WHERE id=?", (a,))

    def test_set_role_and_guards(self):
        with db(self.dsn) as con:
            a, b = self._two_users(con)
            self.assertEqual(admin.set_user_role(con, a, "admin"), "admin")  # admin key (no acting user)
            self.assertEqual({u["id"]: u["role"] for u in admin.list_users(con)["items"]}, {a: "admin", b: "user"})
            with self.assertRaises(admin.AdminError) as cm:
                admin.set_user_role(con, a, "root")
            self.assertEqual(cm.exception.status, 400)
            with self.assertRaises(admin.AdminError) as cm:  # no self-service: not even self-demotion
                admin.set_user_role(con, a, "user", actor_user_id=a)
            self.assertEqual((cm.exception.status, cm.exception.detail), (400, "You cannot change your own role"))
            self.assertEqual(admin.set_user_role(con, b, "admin", actor_user_id=a), "admin")  # an admin appoints another
            self.assertEqual(admin.set_user_role(con, a, "user", actor_user_id=b), "user")
            with self.assertRaises(admin.AdminError) as cm:
                admin.set_user_role(con, 9999, "admin")
            self.assertEqual(cm.exception.status, 404)

    def test_disabled_account_cannot_be_made_admin_and_nobody_disables_themselves(self):
        with db(self.dsn) as con:
            a, b = self._two_users(con)
            admin.set_user_disabled(con, b, True)
            with self.assertRaises(admin.AdminError) as cm:
                admin.set_user_role(con, b, "admin")
            self.assertEqual(cm.exception.status, 400)
            admin.set_user_role(con, a, "admin")
            with self.assertRaises(admin.AdminError) as cm:
                admin.set_user_disabled(con, a, True, actor_user_id=a)
            self.assertEqual((cm.exception.status, cm.exception.detail), (400, "You cannot disable your own account"))
            self.assertEqual(admin.set_user_disabled(con, b, False, actor_user_id=a), 0)  # enabling is always fine


class TestHealth(TempDB):
    def test_reports_booleans_never_secrets(self):
        env = {"BRAVE_API_KEY": "brave-secret-123", "XAI_API_KEY": "xai-secret-456", "MAVE_SMTP_HOST": "smtp.example.org", "MAVE_JWT_SECRET": "jwt-secret-789"}
        with mock.patch.dict(os.environ, env):
            with db(self.dsn) as con:
                h = admin.system_health(con, "9.9.9")
        self.assertEqual(h["version"], "9.9.9")
        self.assertEqual(h["schema_version"], SCHEMA_VERSION)
        self.assertEqual(h["providers"], {"web_search": True, "ai": "xai", "smtp": True})
        blob = json.dumps(h)
        for secret in ("brave-secret-123", "xai-secret-456", "jwt-secret-789", "smtp.example.org"):
            self.assertNotIn(secret, blob)

    def test_unconfigured(self):
        clean = {k: "" for k in ("BRAVE_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY", "MAVE_SMTP_HOST", "MAVE_AI_PROVIDER")}
        with mock.patch.dict(os.environ, clean):
            with db(self.dsn) as con:
                h = admin.system_health(con, "x")
        self.assertEqual(h["providers"], {"web_search": False, "ai": None, "smtp": False})


class TestMetrics(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def test_render_prometheus_text(self):
        metrics.observe("GET", "/search", 200, 0.25)
        metrics.observe("GET", "/search", 200, 0.75)
        metrics.observe("POST", "/auth/login", 401, 0.01)
        out = metrics.render({"mave_documents": 7})
        self.assertIn('mave_http_requests_total{method="GET",route="/search",status="2xx"} 2', out)
        self.assertIn('mave_http_requests_total{method="POST",route="/auth/login",status="4xx"} 1', out)
        self.assertIn('mave_http_request_seconds_sum{method="GET",route="/search"} 1.000000', out)
        self.assertIn('mave_http_request_seconds_count{method="GET",route="/search"} 2', out)
        self.assertIn("mave_documents 7", out)
        self.assertTrue(out.endswith("\n"))

    def test_label_escaping_and_bounded_series(self):
        metrics.observe("GET", 'a"b\\c', 200, 0.0)
        self.assertIn('route="a\\"b\\\\c"', metrics.render())
        metrics.reset()
        with mock.patch.object(metrics, "_MAX_SERIES", 3):
            for i in range(10):
                metrics.observe("GET", f"/r{i}", 200, 0.0)
            out = metrics.render()
        self.assertIn('route="other"', out)
        self.assertLessEqual(out.count("mave_http_requests_total{"), 4)


class TestLogSetup(unittest.TestCase):
    def test_json_formatter_includes_extras_and_is_one_line(self):
        rec = logging.LogRecord("mave.access", logging.INFO, __file__, 1, "request", (), None)
        rec.route, rec.status = "/search", 200
        line = logsetup.JsonFormatter().format(rec)
        data = json.loads(line)
        self.assertNotIn("\n", line)
        self.assertEqual((data["logger"], data["msg"], data["route"], data["status"]), ("mave.access", "request", "/search", 200))
        self.assertRegex(data["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_json_default_only_in_production(self):
        self.assertTrue(logsetup.wants_json("production", ""))
        self.assertFalse(logsetup.wants_json("development", ""))
        self.assertTrue(logsetup.wants_json("development", "json"))
        self.assertFalse(logsetup.wants_json("production", "text"))


class TestWebAppIsCspSafe(unittest.TestCase):
    """The web CSP forbids inline script/style and eval; these checks keep the static files compatible with it."""

    def files(self, pattern):
        found = sorted(WEB.glob(pattern))
        self.assertTrue(found, pattern)
        return found

    def test_html_has_no_inline_script_style_or_handlers(self):
        for f in self.files("*.html"):
            html = f.read_text(encoding="utf-8")
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>", f.name)
            self.assertNotRegex(html, r"<style\b", f.name)
            self.assertNotRegex(html, r"\sstyle\s*=", f.name)
            self.assertNotRegex(html, r"\son[a-z]+\s*=", f.name)
            self.assertNotIn("javascript:", html.lower(), f.name)

    def test_js_avoids_dangerous_sinks(self):
        banned = [r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*=", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new Function\(", r"setAttribute\(\s*['\"]style['\"]"]
        for f in self.files("*.js"):
            src = f.read_text(encoding="utf-8")
            for pat in banned:
                self.assertIsNone(re.search(pat, src), f"{f.name}: {pat}")

    def test_scripts_referenced_by_html_exist(self):
        for f in self.files("*.html"):
            for src in re.findall(r'<script[^>]+src="([^"]+)"', f.read_text(encoding="utf-8")):
                self.assertTrue((WEB / src).is_file(), f"{f.name} -> {src}")

    def test_no_provider_keys_in_client_code(self):
        for f in self.files("*.js"):
            src = f.read_text(encoding="utf-8")
            for needle in ("XAI_API_KEY", "BRAVE_API_KEY", "ANTHROPIC_API_KEY", "x-api-key", "api.x.ai", "api.search.brave.com", "api.anthropic.com"):
                self.assertNotIn(needle, src, f"{f.name}: {needle}")


@unittest.skipUnless(HAVE_API, "fastapi not installed")
class TestAdminAPI(unittest.TestCase):
    H = {"X-Admin-Key": "adminkey"}

    @classmethod
    def setUpClass(cls):
        cls.dsn = create_database()
        cls.addClassCleanup(drop_database, cls.dsn)
        os.environ.update({"MAVE_DATABASE_URL": cls.dsn, "MAVE_ADMIN_KEY": "adminkey", "MAVE_RATE_LIMIT": "100000", "MAVE_AUTH_RATE_LIMIT": "100000"})
        for k in ("BRAVE_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "MAVE_AI_PROVIDER"):
            os.environ.pop(k, None)
        from app.main import app

        cls.c = TestClient(app)

    def test_every_admin_route_needs_the_key(self):
        c = self.c
        for method, path in [
            ("get", "/api/v1/admin/stats"), ("get", "/api/v1/admin/health"), ("get", "/api/v1/admin/analytics"),
            ("get", "/api/v1/admin/crawl-errors"), ("get", "/api/v1/admin/seeds"), ("get", "/api/v1/admin/users"),
            ("get", "/api/v1/admin/audit"), ("get", "/api/v1/admin/metrics"), ("post", "/api/v1/admin/crawl"),
            ("post", "/api/v1/admin/users/1/revoke-sessions"),
            ("post", "/api/v1/admin/users/1/disable"), ("post", "/api/v1/admin/users/1/enable"),
            ("post", "/api/v1/admin/users/1/role"),
        ]:
            self.assertEqual(getattr(c, method)(path).status_code, 403, path)
            self.assertEqual(getattr(c, method)(path, headers={"X-Admin-Key": "wrong"}).status_code, 403, path)

    def test_seed_lifecycle_is_audited(self):
        c, h = self.c, self.H
        r = c.post("/api/v1/admin/seeds", json={"url": "https://om.wikipedia.org/"}, headers=h)
        self.assertEqual(r.status_code, 200)
        url = r.json()["url"]
        self.assertEqual(c.post("/api/v1/admin/seeds", json={"url": "ftp://nope.example/"}, headers=h).status_code, 400)
        self.assertEqual([s["url"] for s in c.get("/api/v1/admin/seeds", headers=h).json()["items"]], [url])
        self.assertEqual(c.put("/api/v1/admin/seeds", json={"url": url, "enabled": False}, headers=h).status_code, 200)
        self.assertEqual(c.post("/api/v1/admin/crawl", json={}, headers=h).status_code, 400)  # no enabled seeds left
        self.assertEqual(c.delete("/api/v1/admin/seeds", params={"url": url}, headers=h).status_code, 200)
        self.assertEqual(c.delete("/api/v1/admin/seeds", params={"url": url}, headers=h).status_code, 404)
        actions = [a["action"] for a in c.get("/api/v1/admin/audit", headers=h).json()["items"]]
        for a in ("seed.add", "seed.disable", "seed.remove"):
            self.assertIn(a, actions)

    def test_users_list_and_revoke(self):
        c, h = self.c, self.H
        r = c.post("/api/v1/auth/register", json={"email": "admin-test@example.com", "password": "password123"})
        token = {"Authorization": "Bearer " + r.json()["token"]}
        self.assertEqual(c.get("/api/v1/me", headers=token).status_code, 200)
        users = c.get("/api/v1/admin/users", params={"q": "admin-test"}, headers=h).json()
        self.assertEqual(users["total"], 1)
        self.assertNotIn("pw_hash", users["items"][0])
        uid = users["items"][0]["id"]
        self.assertEqual(c.post(f"/api/v1/admin/users/{uid}/revoke-sessions", headers=h).json()["revoked"], 1)
        self.assertEqual(c.get("/api/v1/me", headers=token).status_code, 401)  # access ended immediately
        self.assertEqual(c.post("/api/v1/admin/users/99999/revoke-sessions", headers=h).status_code, 404)

    def _signed_in(self, email):
        r = self.c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
        self.assertEqual(r.status_code, 200)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def _user_id(self, email):
        return self.c.get("/api/v1/admin/users", params={"q": email}, headers=self.H).json()["items"][0]["id"]

    def test_admin_role_user_uses_the_admin_api_and_is_audited_by_id(self):
        c, h = self.c, self.H
        boss, other = self._signed_in("role-boss@example.com"), self._signed_in("role-other@example.com")
        boss_id, other_id = self._user_id("role-boss@example.com"), self._user_id("role-other@example.com")
        self.assertEqual(c.get("/api/v1/admin/stats", headers=boss).status_code, 403)  # signed in, but only a user
        self.assertEqual(c.post(f"/api/v1/admin/users/{boss_id}/role", json={"role": "root"}, headers=h).status_code, 422)
        self.assertEqual(c.post("/api/v1/admin/users/99999/role", json={"role": "admin"}, headers=h).status_code, 404)
        r = c.post(f"/api/v1/admin/users/{boss_id}/role", json={"role": "admin"}, headers=h)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "role": "admin"}))
        self.assertEqual(c.get("/api/v1/admin/stats", headers=boss).status_code, 200)  # same token, now an admin
        self.assertEqual(c.get("/api/v1/admin/stats", headers=other).status_code, 403)
        self.assertEqual(c.post(f"/api/v1/admin/users/{other_id}/revoke-sessions", headers=boss).status_code, 200)
        audit = [(a["actor"], a["action"]) for a in c.get("/api/v1/admin/audit", headers=h).json()["items"]]
        self.assertIn((f"user:{boss_id}", "user.revoke_sessions"), audit)  # attributed to the admin user ...
        self.assertIn(("admin-key", "user.role"), audit)  # ... and the appointment to the key that made it
        # nobody changes their own role or disables themselves
        self.assertEqual(c.post(f"/api/v1/admin/users/{boss_id}/role", json={"role": "user"}, headers=boss).status_code, 400)
        self.assertEqual(c.post(f"/api/v1/admin/users/{boss_id}/disable", headers=boss).status_code, 400)
        self.assertEqual(c.get("/api/v1/admin/stats", headers=boss).status_code, 200)
        # the admin key (break-glass) can always take the role away, and it ends at once
        self.assertEqual(c.post(f"/api/v1/admin/users/{boss_id}/role", json={"role": "user"}, headers=h).status_code, 200)
        self.assertEqual(c.get("/api/v1/admin/stats", headers=boss).status_code, 403)

    def test_disabled_or_signed_out_admin_user_is_locked_out(self):
        c, h = self.c, self.H
        tok = self._signed_in("role-locked@example.com")
        uid = self._user_id("role-locked@example.com")
        c.post(f"/api/v1/admin/users/{uid}/role", json={"role": "admin"}, headers=h)
        self.assertEqual(c.get("/api/v1/admin/health", headers=tok).status_code, 200)
        c.post(f"/api/v1/admin/users/{uid}/disable", headers=h)
        self.assertEqual(c.get("/api/v1/admin/health", headers=tok).status_code, 403)
        self.assertEqual(c.post(f"/api/v1/admin/users/{uid}/role", json={"role": "admin"}, headers=tok).status_code, 403)
        c.post(f"/api/v1/admin/users/{uid}/enable", headers=h)
        self.assertEqual(c.get("/api/v1/admin/health", headers=tok).status_code, 403)  # its session was ended by disable

    def test_a_garbage_or_wrong_type_bearer_is_forbidden(self):
        c = self.c
        for token in ("nope", "adminkey", "a.b.c"):
            self.assertEqual(c.get("/api/v1/admin/stats", headers={"Authorization": "Bearer " + token}).status_code, 403, token)
        self.assertEqual(c.get("/api/v1/admin/stats", headers={"X-Admin-Key": ""}).status_code, 403)  # empty key = no key

    def test_health_analytics_metrics(self):
        c, h = self.c, self.H
        c.get("/health")
        health = c.get("/api/v1/admin/health", headers=h).json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["providers"]["ai"], None)
        self.assertNotIn("adminkey", json.dumps(health))
        self.assertIn("top_queries", c.get("/api/v1/admin/analytics", headers=h).json())
        m = c.get("/api/v1/admin/metrics", headers=h)
        self.assertEqual(m.status_code, 200)
        self.assertIn("mave_http_requests_total", m.text)
        self.assertIn("mave_documents", m.text)
        self.assertNotIn("q=", m.text)  # query strings are never used as labels
        bearer = c.get("/api/v1/admin/metrics", headers={"Authorization": "Bearer adminkey"})
        self.assertEqual(bearer.status_code, 200)  # Prometheus `authorization:` block
        self.assertEqual(c.get("/api/v1/admin/metrics", headers={"Authorization": "Bearer nope"}).status_code, 403)
        self.assertEqual(c.get("/api/v1/admin/stats", headers={"Authorization": "Bearer adminkey"}).status_code, 403)  # Bearer is metrics-only

    def test_non_ascii_admin_key_is_forbidden_not_a_server_error(self):
        r = self.c.get("/api/v1/admin/stats", headers={"X-Admin-Key": "k\u00e9y".encode("latin-1")})
        self.assertEqual(r.status_code, 403)

    def test_web_app_is_served_with_a_strict_csp(self):
        c = self.c
        r = c.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertEqual(r.headers["location"], "/web/")
        page = c.get("/web/")
        self.assertEqual(page.status_code, 200)
        csp = page.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(c.get("/web/admin.html").status_code, 200)
        self.assertEqual(c.get("/web/app.js").status_code, 200)
        self.assertEqual(c.get("/web/../app/config.py").status_code, 404)  # no path traversal into the backend source
        self.assertIn("default-src 'none'", c.get("/health").headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
