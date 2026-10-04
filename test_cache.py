"""SharedCache tests (no PostgreSQL, no real Redis): Redis is replaced by a tiny fake key-value store."""
import asyncio
import json
import sys
import types
import unittest
from unittest import mock

from app.core.cache import MAX_VALUE_BYTES, SharedCache, build_cache


class FakeKV:
    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False
        self.calls = 0

    def get(self, k):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(k)

    def set(self, k, v, ex=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        self.data[k] = v.encode() if isinstance(v, str) else v  # real Redis hands back bytes
        self.ttl[k] = ex


class TestLocalOnly(unittest.TestCase):
    def test_roundtrip_miss_and_tuple_keys(self):
        c = SharedCache("t")
        self.assertIsNone(c.get(("q", "web", 20, 0)))
        c.set(("q", "web", 20, 0), [{"title": "a"}])
        self.assertEqual(c.get(("q", "web", 20, 0)), [{"title": "a"}])
        self.assertIsNone(c.get(("q", "web", 20, 20)))

    def test_values_are_copied_in_and_out(self):
        c = SharedCache("t")
        v = {"sources": [1, 2]}
        c.set("k", v)
        v["sources"].clear()  # caller mutates what it stored ...
        got = c.get("k")
        self.assertEqual(got, {"sources": [1, 2]})
        got["sources"].clear()  # ... and what it got back
        self.assertEqual(c.get("k"), {"sources": [1, 2]})

    def test_entries_expire(self):
        with mock.patch("app.core.cache.time.time", return_value=1000.0):
            c = SharedCache("t", ttl=60)
            c.set("k", "v")
        with mock.patch("app.core.cache.time.time", return_value=1030.0):
            self.assertEqual(c.get("k"), "v")
        with mock.patch("app.core.cache.time.time", return_value=1061.0):
            self.assertIsNone(c.get("k"))

    def test_max_size_evicts_oldest(self):
        c = SharedCache("t", maxsize=2)
        for i in range(3):
            c.set(str(i), i)
        self.assertIsNone(c.get("0"))
        self.assertEqual(c.get("2"), 2)


class TestWithRedis(unittest.TestCase):
    def setUp(self):
        self.kv = FakeKV()
        self.now = 0.0
        self.mk = lambda ns="answer", **kw: SharedCache(ns, ttl=300, client=self.kv, clock=lambda: self.now, **kw)

    def test_two_workers_share_entries(self):
        a, b = self.mk(), self.mk()
        a.set("question", {"answer": "Fact [1]."})
        self.assertEqual(b.get("question"), {"answer": "Fact [1]."})  # b never saw it locally
        calls = self.kv.calls
        self.assertEqual(b.get("question"), {"answer": "Fact [1]."})  # now served from b's own memory
        self.assertEqual(self.kv.calls, calls)

    def test_namespaces_do_not_collide(self):
        self.mk("answer").set("k", "A")
        self.assertIsNone(self.mk("providers").get("k"))

    def test_redis_keys_hide_the_query_and_carry_the_ttl(self):
        self.mk().set(("secret political question", "web", 20, 0), ["x"])
        (key,) = self.kv.data
        self.assertTrue(key.startswith("mave:cache:answer:"))
        self.assertNotIn("secret", key)
        self.assertEqual(self.kv.ttl[key], 300)

    def test_values_are_stored_as_json_not_pickle(self):
        self.mk().set("k", {"a": "Afaan Oromoo — ɓ"})
        (raw,) = self.kv.data.values()
        self.assertEqual(json.loads(raw), {"a": "Afaan Oromoo — ɓ"})

    def test_corrupt_or_foreign_entry_is_a_miss_and_not_an_outage(self):
        c = self.mk()
        c.set("k", "v")
        (key,) = self.kv.data
        other = self.mk()
        self.kv.data[key] = b"\x80not json"
        self.assertIsNone(other.get("k"))
        self.assertEqual(other._down_until, 0.0)  # breaker untouched

    def test_unserialisable_and_oversized_values_stay_in_process(self):
        c = self.mk()
        c.set("obj", {"s": {1, 2}})  # a set is not JSON
        c.set("big", "x" * (MAX_VALUE_BYTES + 1))
        self.assertEqual(self.kv.data, {})
        self.assertEqual(len(c.get("big")), MAX_VALUE_BYTES + 1)

    def test_outage_never_raises_keeps_local_cache_and_recovers(self):
        c = self.mk()
        self.kv.fail = True
        c.set("k", "v")  # Redis write fails silently
        self.assertEqual(c.get("k"), "v")  # still served from memory
        self.assertIsNone(c.get("other"))  # miss, no exception
        calls = self.kv.calls
        c.get("another")
        c.set("again", 1)
        self.assertEqual(self.kv.calls, calls)  # breaker: Redis not touched during the outage window
        self.kv.fail = False
        self.now = 11.0  # past retry_after
        c.set("later", 2)
        self.assertIn(c._rkey(c._key("later")), self.kv.data)

    def test_async_api(self):
        a, b = self.mk(), self.mk()

        async def run():
            self.assertIsNone(await a.aget("q"))
            await a.aset("q", {"n": 1})
            self.assertEqual(await b.aget("q"), {"n": 1})
            self.kv.fail = True
            await a.aset("q2", 2)  # must not raise
            self.assertEqual(await a.aget("q2"), 2)

        asyncio.run(run())


class TestBuildCache(unittest.TestCase):
    def test_without_url_is_in_process(self):
        with mock.patch.dict("os.environ", {"MAVE_REDIS_URL": ""}):
            self.assertIsNone(build_cache("x").client)

    def test_with_url_uses_redis_client(self):
        fake = types.ModuleType("redis")
        fake.Redis = types.SimpleNamespace(from_url=lambda url, **kw: FakeKV())
        with mock.patch.dict(sys.modules, {"redis": fake}), mock.patch.dict("os.environ", {"MAVE_REDIS_URL": "redis://cache:6379/0"}):
            self.assertIsInstance(build_cache("x").client, FakeKV)

    def test_missing_redis_package_degrades_to_in_process(self):
        with mock.patch.dict(sys.modules, {"redis": None}), mock.patch.dict("os.environ", {"MAVE_REDIS_URL": "redis://cache:6379/0"}):
            self.assertIsNone(build_cache("x").client)


if __name__ == "__main__":
    unittest.main()
