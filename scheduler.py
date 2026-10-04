"""Recrawl scheduling: adaptive per-document interval (changed pages are revisited sooner, stable ones later)."""
from ..db import Conn

DEFAULT_INTERVAL = 7 * 86400.0
MIN_INTERVAL = 6 * 3600.0
MAX_INTERVAL = 60 * 86400.0
RECRAWL_PRIORITY = 30.0


def clamp(interval: float) -> float:
    return max(MIN_INTERVAL, min(MAX_INTERVAL, interval))


def next_interval(current: float | None, changed: bool) -> float:
    """Halve the interval after a change, double it after an unchanged visit (bounded)."""
    cur = current or DEFAULT_INTERVAL
    return clamp(cur / 2 if changed else cur * 2)


def enqueue_due(con: Conn, now: float, limit: int = 500) -> list[str]:
    """Put documents whose next_crawl_at has passed back into the frontier. Returns the queued URLs."""
    urls = [
        r["url"]
        for r in con.execute(
            "SELECT url FROM documents WHERE next_crawl_at IS NOT NULL AND next_crawl_at <= ? "
            "ORDER BY next_crawl_at LIMIT ?",
            (now, limit),
        )
    ]
    for u in urls:
        con.execute(
            """INSERT INTO frontier(url, depth, status, added_at, priority) VALUES(?,0,'pending',?,?)
               ON CONFLICT(url) DO UPDATE SET status='pending', attempts=0, next_at=0, priority=excluded.priority,
                                              last_error=NULL
               WHERE frontier.status <> 'working'""",
            (u, now, RECRAWL_PRIORITY),
        )
    return urls
