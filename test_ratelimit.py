"""Rate-limiter tests. They need neither PostgreSQL nor a Redis server: Redis is replaced by a tiny fake that supports
the few commands the limiter uses (pipeline of INCR / EXPIRE / GET) and honours expiry against a settable clock."""
import sys
import types
import unittest
from unittest import mock

from app.core import ratelimit
from app.core.config import settings
from app.core.ratelimit import MemoryLimiter, RedisLimiter, build_limiter


class FakeRedis:
    def __init__(self):
        self.clock = 0.0
        self.data: dict[str, list] = {}  # key -> [value, expires_at]
        self.fail = False
        self.pipelines = 0

    def _live(self, key):
        item = self.data.get(key)
        if item and item[1] is not None and item[1] <= self.clock:
            del self.data[key]
            return None
        return item

    def pipeline(self, transaction=True):
        if self.fail:
            raise ConnectionError("redis down")
        self.pipelines += 1
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def incr(self, k):
        self.ops.append(("incr", k))

    def expire(self, k, ttl):
        self.ops.append(("expire", k, ttl))

    def get(self, k):
        self.ops.append(("get", k))

    def execute(self):
        if self.r.fail:
            raise ConnectionError("redis down")
        out = []
        for op, k, *rest in self.ops:
            if op == "incr":
                item = self.r._live(k) or self.r.data.setdefault(k, [0, None])
                item[0] += 1
                out.append(item[0])
            elif op == "expire":
                item = self.r._live(k)
                if item:
                    item[1] = self.r.clock + rest[0]
                out.append(bool(item))
            else:
                item = self.r._live(k)
                out.append(str(item[0]).encode() if item else None)  # real Redis returns bytes
        return out


class TestMemoryLimiter(unittest.TestCase):
    def test_blocks_after_limit_and_recovers_after_the_window(self):
        m = MemoryLimiter()
        self.assertTrue(all(m.allow("a", 3, now=100.0 + i) for i in range(3)))
        self.assertFalse(m.allow("a", 3, now=103.0))
        self.assertTrue(m.allow("b", 3, now=103.0))  # other clients are independent
        self.assertTrue(m.allow("a", 3, now=161.0))  # the first hit (t=100) left the 60 s window

    def test_idle_clients_are_purged(self):
        m = MemoryLimiter(max_keys=5)
        for i in range(10):
            m.allow(f"ip{i}", 5, now=0.0)
        m.allow("late", 5, now=1000.0)
        self.assertLessEqual(len(m._hits), 2)


class TestRedisLimiter(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()
        self.lim = RedisLimiter(self.r)

    def at(self, t):
        self.r.clock = t
        return t

    def test_allows_exactly_limit_requests_in_a_window(self):
        results = [self.lim.allow("1.2.3.4", 5, now=self.at(120.0 + i * 0.1)) for i in range(7)]
        self.assertEqual(results, [True] * 5 + [False] * 2)

    def test_clients_are_independent(self):
        for _ in range(3):
            self.lim.allow("a", 3, now=self.at(120.0))
        self.assertFalse(self.lim.allow("a", 3, now=self.at(120.0)))
        self.assertTrue(self.lim.allow("b", 3, now=self.at(120.0)))

    def test_previous_window_counts_less_as_time_passes(self):
        for _ in range(10):  # fill the window [120, 180)
            self.lim.allow("a", 10, now=self.at(125.0))
        # just after the boundary the previous window still weighs almost fully ...
        self.assertFalse(self.lim.allow("a", 10, now=self.at(181.0)))
        # ... and it has faded by the end of the next window
        self.assertTrue(self.lim.allow("a", 10, now=self.at(239.0)))

    def test_counters_expire_so_redis_does_not_grow(self):
        self.lim.allow("a", 5, now=self.at(120.0))
        self.assertTrue(self.r.data)
        self.r.clock = 120.0 + 121
        self.assertIsNone(self.r._live(next(iter(self.r.data))))

    def test_keys_never_contain_the_raw_ip(self):
        self.lim.allow("203.0.113.9", 5, now=self.at(120.0))
        self.assertTrue(self.r.data)
        for key in self.r.data:
            self.assertNotIn("203.0.113.9", key)
            self.assertTrue(key.startswith("mave:rl:"))
        self.assertTrue(self.lim.allow("x" * 5000, 5, now=self.at(120.0)))  # oversized X-Forwarded-For stays bounded
        self.assertTrue(all(len(k) < 80 for k in self.r.data))

    def test_redis_outage_falls_back_to_process_limits_without_error(self):
        self.r.fail = True
        results = [self.lim.allow("a", 2, now=200.0 + i) for i in range(4)]
        self.assertEqual(results, [True, True, False, False])  # per-process limit still protects

    def test_circuit_breaker_stops_calling_redis_during_the_outage_then_recovers(self):
        self.r.fail = True
        self.lim.allow("a", 5, now=300.0)
        self.r.fail = False
        before = self.r.pipelines
        self.lim.allow("a", 5, now=305.0)  # inside retry_after (10 s): Redis is not touched
        self.assertEqual(self.r.pipelines, before)
        self.r.clock = 311.0
        self.assertTrue(self.lim.allow("a", 5, now=311.0))  # after retry_after Redis is used again
        self.assertGreater(self.r.pipelines, before)

    def test_garbage_from_redis_degrades_instead_of_raising(self):
        class Bad:
            def pipeline(self, transaction=True):
                p = mock.Mock()
                p.execute.return_value = [1, True, b"not-a-number"]
                return p

        lim = RedisLimiter(Bad())
        self.assertTrue(lim.allow("a", 5, now=1000.0))  # falls back to memory, no exception


class TestBuildLimiter(unittest.TestCase):
    def test_no_url_means_memory(self):
        self.assertIsInstance(build_limiter(""), MemoryLimiter)
        self.assertIsInstance(build_limiter("   "), MemoryLimiter)

    def test_unsupported_scheme_means_memory(self):
        self.assertIsInstance(build_limiter("http://cache:6379"), MemoryLimiter)
        self.assertIsInstance(build_limiter("cache:6379"), MemoryLimiter)

    def test_missing_package_means_memory(self):
        with mock.patch.dict(sys.modules, {"redis": None}):  # makes `import redis` raise ImportError
            self.assertIsInstance(build_limiter("redis://cache:6379/0"), MemoryLimiter)

    def test_builds_a_redis_limiter_with_short_timeouts_and_never_logs_the_url(self):
        calls = {}
        fake_mod = types.ModuleType("redis")
        fake_mod.Redis = types.SimpleNamespace(from_url=lambda url, **kw: calls.update(url=url, **kw) or FakeRedis())
        with mock.patch.dict(sys.modules, {"redis": fake_mod}):
            lim = build_limiter("redis://:s3cret@cache:6379/0")
        self.assertIsInstance(lim, RedisLimiter)
        self.assertLessEqual(calls["socket_timeout"], 0.5)
        self.assertLessEqual(calls["socket_connect_timeout"], 0.5)

    def test_bad_url_that_the_client_rejects_means_memory_and_no_secret_in_logs(self):
        def boom(url, **kw):
            raise ValueError(f"cannot parse {url}")

        fake_mod = types.ModuleType("redis")
        fake_mod.Redis = types.SimpleNamespace(from_url=boom)
        with mock.patch.dict(sys.modules, {"redis": fake_mod}), self.assertLogs("mave.ratelimit", "WARNING") as cm:
            lim = build_limiter("redis://:s3cret@cache:6379/0")
        self.assertIsInstance(lim, MemoryLimiter)
        self.assertNotIn("s3cret", " ".join(cm.output))


class TestRedisConfig(unittest.TestCase):
    def test_redis_url_setting_is_read_at_access_time(self):
        with mock.patch.dict("os.environ", {"MAVE_REDIS_URL": " redis://cache:6379/0 "}):
            self.assertEqual(settings.redis_url, "redis://cache:6379/0")
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("MAVE_REDIS_URL", None)
            self.assertEqual(settings.redis_url, "")

    def test_production_without_redis_gets_a_warning_with_it_does_not(self):
        env = {"MAVE_ENV": "production"}
        with mock.patch.dict("os.environ", env):
            import os

            os.environ.pop("MAVE_REDIS_URL", None)
            self.assertIn("MAVE_REDIS_URL", " ".join(settings.warnings()))
        with mock.patch.dict("os.environ", {**env, "MAVE_REDIS_URL": "redis://cache:6379/0"}):
            self.assertNotIn("MAVE_REDIS_URL", " ".join(settings.warnings()))


if __name__ == "__main__":
    unittest.main()
