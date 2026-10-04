"""Brave results are cached through SharedCache: one upstream call serves repeated and cross-worker requests.
Needs httpx importable (the upstream call itself is faked)."""
import asyncio
import importlib.util
import unittest
from unittest import mock

from tests.test_cache import FakeKV

HAVE_HTTPX = importlib.util.find_spec("httpx") is not None


class _Resp:
    status_code = 200

    def json(self):
        return {"web": {"results": [{"title": "<b>Hi</b>", "url": "https://a.test/1", "description": "d <i>x</i>"}]}}


def _fake_client(counter):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            counter.append(1)
            return _Resp()

    return C


@unittest.skipUnless(HAVE_HTTPX, "httpx not installed")
class TestProviderCache(unittest.TestCase):
    def setUp(self):
        from app.core.cache import SharedCache
        from app.search import web

        self.web, self.kv, self.calls = web, FakeKV(), []
        self.cache_a = SharedCache("providers", ttl=300, client=self.kv)  # worker A
        self.cache_b = SharedCache("providers", ttl=300, client=self.kv)  # worker B
        p = mock.patch.dict("os.environ", {"BRAVE_API_KEY": "k", "MAVE_ENV": "development"})
        p.start()
        self.addCleanup(p.stop)
        c = mock.patch.object(web.httpx, "AsyncClient", _fake_client(self.calls))
        c.start()
        self.addCleanup(c.stop)

    def search(self, cache, q="oromia", **kw):
        with mock.patch.object(self.web, "_cache", cache):
            return asyncio.run(self.web.web_search(q, "web", **kw))

    def test_second_request_is_served_from_cache(self):
        first = self.search(self.cache_a)
        again = self.search(self.cache_a)
        self.assertEqual((len(self.calls), first, again), (1, again, first))
        self.assertEqual(first[0]["title"], "Hi")  # markup stripped before caching

    def test_other_worker_reuses_the_result_through_redis(self):
        self.search(self.cache_a)
        self.search(self.cache_b)
        self.assertEqual(len(self.calls), 1)

    def test_page_query_and_kind_are_part_of_the_key(self):
        self.search(self.cache_a, offset=0)
        self.search(self.cache_a, offset=20)
        self.search(self.cache_a, q="other")
        self.assertEqual(len(self.calls), 3)

    def test_callers_cannot_corrupt_the_cached_list(self):
        self.search(self.cache_a)[0]["title"] = "MUTATED"
        self.assertEqual(self.search(self.cache_a)[0]["title"], "Hi")


if __name__ == "__main__":
    unittest.main()
