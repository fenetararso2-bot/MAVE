"""loadtest.py against a local stub server (standard library only, no MAVE server needed)."""
import contextlib
import http.server
import io
import json
import socket
import threading
import unittest
from unittest import mock

import loadtest


class Stub(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, status=200, delay=0.0):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.status, self.delay = status, delay
        self.paths, self.connections = [], 0
        self.lock = threading.Lock()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like a real deployment

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def do_GET(self):
        with self.server.lock:
            self.server.paths.append(self.path)
        body = json.dumps({"ok": True}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def serving(**kw):
    srv = Stub(**kw)
    t = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestStats(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        v = list(range(1, 101))  # 1..100
        self.assertEqual([loadtest.percentile(v, p) for p in (50, 90, 95, 99, 100)], [50, 90, 95, 99, 100])
        self.assertEqual(loadtest.percentile([7.0], 99), 7.0)
        self.assertEqual(loadtest.percentile([], 95), 0.0)

    def test_classification(self):
        R = loadtest.Result
        s = loadtest.summarise([R(0.1, 200), R(0.3, 200), R(0.0, 429), R(0.2, 500), R(0.0, 0, "OSError"), R(0.1, 404)], 2.0)
        self.assertEqual((s["total"], s["ok"], s["throttled_429"], s["errors"]), (6, 2, 1, 3))
        self.assertAlmostEqual(s["error_rate"], 0.5)
        self.assertAlmostEqual(s["rps"], 3.0)
        self.assertEqual(s["by_status"]["connection-error"], 1)
        self.assertAlmostEqual(s["latency_ms"]["max"], 300.0)  # latency counts successful requests only

    def test_verdict(self):
        base = {"total": 10, "ok": 10, "error_rate": 0.0, "latency_ms": {"p95": 500.0}}
        self.assertEqual(loadtest.verdict(base, 800, 0.01), [])
        self.assertTrue(any("p95" in p for p in loadtest.verdict(base, 300, None)))
        self.assertTrue(any("error rate" in p for p in loadtest.verdict({**base, "error_rate": 0.2}, None, 0.01)))
        self.assertTrue(loadtest.verdict({**base, "ok": 0, "latency_ms": {"p95": 0.0}}, None, None))
        self.assertTrue(loadtest.verdict({**base, "total": 0, "ok": 0}, None, None))

    def test_request_mix_covers_all_endpoints_and_stays_url_safe(self):
        import random

        rng = random.Random(1)
        paths = {loadtest.pick_path(rng) for _ in range(300)}
        for needle in ("/api/v1/search?q=", "/api/v1/suggest?q=", "/api/v1/trending", "/health", "page=2"):
            self.assertTrue(any(needle in p for p in paths), needle)
        self.assertTrue(all(" " not in p for p in paths))


class TestRun(unittest.TestCase):
    def test_fixed_number_of_requests_and_connection_reuse(self):
        with serving() as srv:
            s = loadtest.run_load(srv.url, concurrency=4, requests=40)
        self.assertEqual((s["total"], s["ok"], s["errors"]), (40, 40, 0))
        self.assertEqual(len(srv.paths), 40)
        self.assertLessEqual(srv.connections, 8)  # keep-alive: far fewer connections than requests
        self.assertGreater(s["latency_ms"]["p50"], 0)

    def test_duration_mode_stops(self):
        with serving() as srv:
            s = loadtest.run_load(srv.url, concurrency=2, duration=0.3)
        self.assertGreater(s["total"], 0)
        self.assertLess(s["seconds"], 5)

    def test_server_errors_are_errors(self):
        with serving(status=500) as srv:
            s = loadtest.run_load(srv.url, concurrency=2, requests=10)
        self.assertEqual((s["errors"], s["ok"], s["error_rate"]), (10, 0, 1.0))

    def test_429_is_throttled_not_error_but_run_still_fails(self):
        with serving(status=429) as srv:
            s = loadtest.run_load(srv.url, concurrency=2, requests=10)
        self.assertEqual((s["throttled_429"], s["errors"]), (10, 0))
        self.assertTrue(loadtest.verdict(s, None, None))  # nothing succeeded -> not a pass

    def test_connection_refused_is_counted_not_raised(self):
        s = loadtest.run_load(f"http://127.0.0.1:{free_port()}", concurrency=2, requests=6, timeout=1)
        self.assertEqual((s["total"], s["errors"]), (6, 6))
        self.assertEqual(s["by_status"], {"connection-error": 6})

    def test_base_path_prefix_is_kept(self):
        with serving() as srv:
            loadtest.run_load(srv.url + "/mave/", concurrency=1, requests=5)
        self.assertTrue(all(p.startswith("/mave/") for p in srv.paths))

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            loadtest.run_load("http://127.0.0.1:1")  # neither duration nor requests
        with self.assertRaises(ValueError):
            loadtest.run_load("ftp://x", requests=1)


class TestCli(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = loadtest.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_pass_and_json_output(self):
        with serving() as srv:
            rc, out, _ = self.run_main("--base-url", srv.url, "--requests", "20", "--concurrency", "2", "--json", "--max-error-rate", "0.01")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual((data["total"], data["problems"]), (20, []))

    def test_threshold_failure_exits_1(self):
        with serving(status=500) as srv:
            rc, out, _ = self.run_main("--base-url", srv.url, "--requests", "10", "--max-error-rate", "0.1")
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    def test_refuses_public_hosts_without_the_flag_and_sends_nothing(self):
        with mock.patch.object(loadtest, "run_load") as run:
            rc, _, err = self.run_main("--base-url", "http://8.8.8.8", "--requests", "1")
        self.assertEqual(rc, 2)
        self.assertIn("--i-own-this-server", err)
        run.assert_not_called()

    def test_flag_allows_public_hosts(self):
        with mock.patch.object(loadtest, "run_load", return_value={"total": 1, "ok": 1, "throttled_429": 0, "errors": 0, "error_rate": 0.0,
                                                                 "rps": 1.0, "seconds": 1.0, "by_status": {"200": 1},
                                                                 "latency_ms": {"p50": 1, "p90": 1, "p95": 1, "p99": 1, "max": 1}}) as run:
            rc, _, _ = self.run_main("--base-url", "http://8.8.8.8", "--requests", "1", "--i-own-this-server")
        self.assertEqual(rc, 0)
        run.assert_called_once()

    def test_private_host_detection(self):
        for h in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.0.5", "::1"):
            self.assertTrue(loadtest.is_private_host(h), h)
        for h in ("8.8.8.8", "1.1.1.1"):
            self.assertFalse(loadtest.is_private_host(h), h)

    def test_argument_validation(self):
        for argv in (["--concurrency", "0"], ["--concurrency", "9999"], ["--requests", "0"], ["--base-url", "nope"]):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                loadtest.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)


if __name__ == "__main__":
    unittest.main()
