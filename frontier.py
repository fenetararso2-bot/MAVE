"""URL frontier on PostgreSQL: priority queue, retry with exponential backoff, resumable after a crash."""
from ..db import Conn


def add(con: Conn, url: str, depth: int, priority: float, now: float) -> int:
    cur = con.execute(
        "INSERT INTO frontier(url, depth, status, added_at, priority) VALUES(?,?,'pending',?,?)"
        " ON CONFLICT(url) DO NOTHING",
        (url, depth, now, priority),
    )
    return cur.rowcount


def add_done(con: Conn, url: str, depth: int, now: float) -> None:
    """Record an alias (redirect target / canonical URL) so it is not fetched again."""
    con.execute(
        "INSERT INTO frontier(url, depth, status, added_at) VALUES(?,?,'done',?) ON CONFLICT(url) DO NOTHING",
        (url, depth, now),
    )


def claim_next(con: Conn, now: float):
    row = con.execute(
        """SELECT url, depth, attempts FROM frontier WHERE status='pending' AND next_at <= ?
           ORDER BY priority DESC, depth, added_at LIMIT 1
           FOR UPDATE SKIP LOCKED""",  # several crawler workers never claim the same URL
        (now,),
    ).fetchone()
    if row:
        con.execute("UPDATE frontier SET status='working' WHERE url=?", (row["url"],))
    return row


def seconds_until_ready(con: Conn, now: float) -> float | None:
    """Time until the earliest delayed (retrying) URL becomes ready; None if nothing is waiting."""
    r = con.execute("SELECT MIN(next_at) FROM frontier WHERE status='pending' AND next_at > ?", (now,)).fetchone()[0]
    return None if r is None else max(0.0, r - now)


def reschedule(con: Conn, url: str, attempts: int, next_at: float, error: str) -> None:
    con.execute(
        "UPDATE frontier SET status='pending', attempts=?, next_at=?, last_error=? WHERE url=?",
        (attempts, next_at, error[:300], url),
    )


def mark(con: Conn, url: str, status: str, error: str | None = None) -> None:
    con.execute("UPDATE frontier SET status=?, last_error=? WHERE url=?", (status, error and error[:300], url))


def recover_interrupted(con: Conn) -> None:
    con.execute("UPDATE frontier SET status='pending' WHERE status='working'")


def backoff_delay(attempts: int, base: float, cap: float, retry_after: float | None = None) -> float:
    """base * 2**attempts, capped; a server-provided Retry-After can only make it longer."""
    delay = min(cap, base * (2**attempts))
    if retry_after is not None:
        delay = max(delay, min(retry_after, cap))
    return delay
