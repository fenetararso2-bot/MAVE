"""robots.txt handling: allow/deny, Crawl-delay and Sitemap directives, cached per origin."""
import urllib.error
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

MAX_CRAWL_DELAY = 30.0


class RobotsCache:
    def __init__(self, bot_name: Callable[[], str], raw_fetcher):
        self._bot_name = bot_name
        self._fetch = raw_fetcher
        self._cache: dict[str, RobotFileParser | None] = {}

    @staticmethod
    def _origin(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _load(self, origin: str) -> RobotFileParser | None:
        if origin in self._cache:
            return self._cache[origin]
        rp: RobotFileParser | None = RobotFileParser()
        try:
            data = self._fetch(origin + "/robots.txt", timeout=8, max_bytes=500_000)
            rp.parse(data.decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code < 500:  # 4xx: no robots file -> everything allowed
                rp = None
            else:  # 5xx: be conservative
                rp.disallow_all = True
        except Exception:  # unreachable / blocked / malformed: be conservative
            rp.disallow_all = True
        self._cache[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._load(self._origin(url))
        return True if rp is None else rp.can_fetch(self._bot_name(), url)

    def crawl_delay(self, url: str) -> float | None:
        rp = self._load(self._origin(url))
        if rp is None or rp.disallow_all:
            return None
        delay = rp.crawl_delay(self._bot_name())
        return min(float(delay), MAX_CRAWL_DELAY) if delay else None

    def sitemaps(self, origin: str) -> list[str]:
        rp = self._load(origin)
        return list(rp.site_maps() or []) if rp is not None and not rp.disallow_all else []
