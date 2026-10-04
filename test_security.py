"""Config late-binding, production validation and SSRF protection tests (no FastAPI needed)."""
import asyncio
import http.server
import importlib.util
import os
import socket
import threading
import unittest
import urllib.request
from unittest import mock

from app.core import netguard
from app.core.config import settings
from app.crawler import Crawler, fetch_url
from app.db import db
from tests.pgtest import create_database, drop_database


class TestConfig(unittest.TestCase):
    def test_env_is_read_at_access_time_not_import_time(self):
        # Regression: app.db / app.core.security were already imported (they import `settings`) when the API
        # test set MAVE_ADMIN_KEY, and the old eager dataclass had frozen the empty value -> 403 on admin stats.
        with mock.patch.dict(os.environ, {"MAVE_ADMIN_KEY": "k-one", "MAVE_DATABASE_URL": "postgresql://u:p@db.example/one"}):
            self.assertEqual(settings.admin_key, "k-one")
            self.assertEqual(settings.database_url, "postgresql://u:p@db.example/one")
        with mock.patch.dict(os.environ, {"MAVE_ADMIN_KEY": "k-two"}):
            self.assertEqual(settings.admin_key, "k-two")

    def test_fallback_jwt_secret_is_stable_within_process(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAVE_JWT_SECRET", None)
            self.assertEqual(settings.jwt_secret, settings.jwt_secret)
            self.assertGreaterEqual(len(settings.jwt_secret), 32)

    def test_bad_int_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"MAVE_RATE_LIMIT": "lots"}):
            self.assertEqual(settings.rate_limit_per_min, 90)

    def test_bot_name_has_no_placeholder_url(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAVE_BOT_URL", None)
            self.assertNotIn("example", settings.bot_name)
        with mock.patch.dict(os.environ, {"MAVE_BOT_URL": "https://mave.real/bot"}):
            self.assertIn("https://mave.real/bot", settings.bot_name)

    def test_production_validation_rejects_weak_or_missing_secrets(self):
        env = {"MAVE_JWT_SECRET": "change-me-to-a-long-random-string", "MAVE_ADMIN_KEY": "change-me-too"}
        with mock.patch.dict(os.environ, env):
            problems = " ".join(settings.validate())
            self.assertIn("MAVE_JWT_SECRET", problems)
            self.assertIn("MAVE_ADMIN_KEY", problems)
            self.assertIn("MAVE_DATABASE_URL", problems)  # production must name its PostgreSQL database explicitly
        good = {
            "MAVE_DATABASE_URL": "postgresql://mave:secret@db:5432/mave",
            "MAVE_JWT_SECRET": "a" * 48,
            "MAVE_ADMIN_KEY": "b" * 24,
            "BRAVE_API_KEY": "x",
            "ANTHROPIC_API_KEY": "y",
        }
        with mock.patch.dict(os.environ, good):
            self.assertEqual(settings.validate(), [])


class TestNetGuard(unittest.TestCase):
    BAD = [
        "http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
        "http://[::ffff:127.0.0.1]/", "http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
        "http://100.64.0.1/", "http://0.0.0.0/", "http://2130706433/", "http://0x7f.0.0.1/", "http://127.1/",
        "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[fe80::1]/", "http://[fd00::1]/",
        "file:///etc/passwd", "ftp://example.com/", "gopher://x/", "http://user:pw@example.com/",
        "http://example.com:22/", "http://example.com:6379/", "http://metadata.google.internal/",
        "http://svc.internal/", "http://x.localhost/", "http:///nohost", "javascript:alert(1)",
    ]

    def test_bad_urls_are_blocked(self):
        for u in self.BAD:
            with self.subTest(url=u), self.assertRaises(netguard.BlockedURL):
                netguard.check_url(u)

    def test_public_literals_pass(self):
        for u in ("http://93.184.216.34/", "https://8.8.8.8/x", "https://[2606:4700:4700::1111]/"):
            with self.subTest(url=u):
                self.assertEqual(netguard.check_url(u), u)

    def test_is_public_ip(self):
        for ip in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(netguard.is_public_ip(ip), ip)
        for ip in ("127.0.0.1", "10.1.1.1", "169.254.169.254", "::1", "::ffff:10.0.0.1", "224.0.0.1", "garbage"):
            self.assertFalse(netguard.is_public_ip(ip), ip)

    @staticmethod
    def _dns(mapping):
        def fake(host, port, *a, **k):
            if host not in mapping:
                raise socket.gaierror("nx")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in mapping[host]]
        return fake

    def test_hostname_resolving_to_private_address_is_blocked(self):
        with mock.patch("socket.getaddrinfo", self._dns({"evil.test": ["10.0.0.7"], "ok.test": ["93.184.216.34"]})):
            with self.assertRaises(netguard.BlockedURL):
                netguard.check_url("http://evil.test/")
            self.assertEqual(netguard.check_url("http://ok.test/"), "http://ok.test/")
            with self.assertRaises(netguard.BlockedURL):  # unresolvable host
                netguard.check_url("http://nope.test/")

    def test_mixed_public_and_private_answers_are_blocked(self):
        with mock.patch("socket.getaddrinfo", self._dns({"mix.test": ["93.184.216.34", "127.0.0.1"]})):
            with self.assertRaises(netguard.BlockedURL):
                netguard.check_url("http://mix.test/")

    def test_dns_rebinding_between_check_and_connect_is_refused(self):
        answers = iter([["93.184.216.34"], ["127.0.0.1"]])  # public when checked, loopback when connecting

        def flip(host, port, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in next(answers)]

        with mock.patch("socket.getaddrinfo", flip):
            netguard.check_url("http://rebind.test/")
            with self.assertRaises(netguard.BlockedURL):
                netguard._guarded_socket("rebind.test", 80, 2, None)

    def test_redirect_to_internal_target_is_blocked(self):
        h = netguard._GuardedRedirect()
        req = urllib.request.Request("http://93.184.216.34/start")
        for target in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8080/admin",
                       "/relative/../../etc", "file:///etc/passwd", "http://[::1]/"):
            if target.startswith("/"):
                continue  # relative redirect stays on the (public) origin
            with self.subTest(target=target), self.assertRaises(netguard.BlockedURL):
                h.redirect_request(req, None, 302, "Found", {}, target)
        ok = h.redirect_request(req, None, 302, "Found", {}, "/relative")
        self.assertEqual(ok.full_url, "http://93.184.216.34/relative")


class _Counting(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><title>internal</title>" + b"secret " * 40 + b"</html>")

    def log_message(self, *a):
        pass


class TestSSRFEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Counting)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Counting.hits = 0

    def test_fetch_url_never_reaches_a_loopback_server(self):
        # port is not in ALLOWED_PORTS either way; also prove the host policy alone is enough
        with mock.patch.object(netguard, "ALLOWED_PORTS", {self.port}):
            with self.assertRaises(netguard.BlockedURL):
                fetch_url(f"http://127.0.0.1:{self.port}/")
        self.assertEqual(_Counting.hits, 0)

    def test_crawler_counts_internal_seed_as_blocked_and_indexes_nothing(self):
        path = create_database()
        try:
            with mock.patch.object(netguard, "ALLOWED_PORTS", {self.port}):
                c = Crawler(dsn=path, delay=0, respect_robots=False, max_depth=0)
                res = c.run([f"http://127.0.0.1:{self.port}/"])
            self.assertEqual(res["indexed"], 0)
            self.assertEqual(res["blocked"], 1)
            self.assertEqual(_Counting.hits, 0)
            with db(path) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        finally:
            drop_database(path)


class TestProductionGating(unittest.TestCase):
    def test_warnings_report_missing_providers_and_cors(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("BRAVE_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "MAVE_AI_PROVIDER", "MAVE_CORS_ORIGINS"):
                os.environ.pop(k, None)
            text = " ".join(settings.warnings())
        self.assertIn("BRAVE_API_KEY", text)
        self.assertIn("XAI_API_KEY", text)
        self.assertIn("ANTHROPIC_API_KEY", text)
        self.assertIn("MAVE_CORS_ORIGINS", text)

    def test_auth_limit_never_exceeds_global_limit(self):
        with mock.patch.dict(os.environ, {"MAVE_RATE_LIMIT": "5", "MAVE_AUTH_RATE_LIMIT": "50"}):
            self.assertEqual(settings.auth_rate_limit_per_min, 5)
        with mock.patch.dict(os.environ, {"MAVE_RATE_LIMIT": "100", "MAVE_AUTH_RATE_LIMIT": "10"}):
            self.assertEqual(settings.auth_rate_limit_per_min, 10)

    @unittest.skipUnless(importlib.util.find_spec("httpx"), "httpx not installed")
    def test_no_demo_data_or_answers_in_production(self):
        from app.ai import providers
        from app.core.errors import ProviderError
        from app.search import web

        env = {"MAVE_ENV": "production"}
        with mock.patch.dict(os.environ, env):
            for k in ("BRAVE_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "MAVE_AI_PROVIDER"):
                os.environ.pop(k, None)
            with self.assertRaises(ProviderError):
                asyncio.run(web.web_search("hello"))
            with self.assertRaises(ProviderError):
                asyncio.run(providers.complete("system", "hello"))
        with mock.patch.dict(os.environ, {"MAVE_ENV": "development"}):
            os.environ.pop("BRAVE_API_KEY", None)
            self.assertTrue(asyncio.run(web.web_search("hello")))  # demo data only outside production


if __name__ == "__main__":
    unittest.main()
