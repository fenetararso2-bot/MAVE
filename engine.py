"""Indexing and local retrieval on top of PostgreSQL full-text search (tsvector + GIN)."""
import time

from ..db import Conn
from .oromoo import fts_query_om, looks_oromoo
from .ranking import diversify
from .scoring import RELEVANCE_SCALE  # noqa: F401  (re-exported: it used to live here)
from .scoring import score_candidates
from ..core.textutil import content_tokens, fts_query, host, make_snippet

MAX_BODY = 200_000
# ts_rank_cd weights for {D, C, B, A}: title (A) counts ~6x a body (B) hit, as the old BM25 field weights did.
RANK_WEIGHTS = "{0.05, 0.1, 0.2, 1.0}"


def upsert_document(con: Conn, url: str, title: str, body: str, lang: str | None = None) -> None:
    con.execute(
        """INSERT INTO documents(url, domain, title, body, lang, fetched_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(url) DO UPDATE SET
             title=excluded.title, body=excluded.body, lang=excluded.lang, fetched_at=excluded.fetched_at""",
        (url, host(url), (title or url)[:300], (body or "")[:MAX_BODY], lang, time.time()),
    )


def add_link_counts(con: Conn, urls: list[str]) -> None:
    # sorted: concurrent crawlers lock the rows in the same order, so they cannot deadlock each other
    con.executemany(
        "INSERT INTO link_counts(url, n) VALUES(?,1) ON CONFLICT(url) DO UPDATE SET n = link_counts.n + 1",
        [(u,) for u in sorted(set(urls))],
    )


def refresh_vocab(con: Conn) -> None:
    """Rebuild the spelling vocabulary (``docs_vocab``) from the index. Readers are not blocked."""
    con.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY docs_vocab")


def _run(con: Conn, match: str, limit: int, lang: str | None):
    """Best ``limit`` matches for a tsquery expression. The (large) body is not fetched here."""
    sql = f"""
        SELECT d.url, d.domain, d.title, d.lang, d.fetched_at,
               COALESCE(l.n, 0) AS inlinks,
               ts_rank_cd('{RANK_WEIGHTS}', d.tsv, q.query, 32) AS rank
        FROM documents d
        CROSS JOIN (SELECT to_tsquery('simple', mave_unaccent(?)) AS query) q
        LEFT JOIN link_counts l ON l.url = d.url
        WHERE d.tsv @@ q.query
    """
    args: list = [match]
    if lang:
        sql += " AND d.lang = ?"
        args.append(lang)
    sql += " ORDER BY rank DESC, d.id LIMIT ?"
    args.append(limit)
    return con.execute(sql, args).fetchall()


def _bodies(con: Conn, urls: list[str]) -> dict[str, str]:
    if not urls:
        return {}
    return {r["url"]: r["body"] for r in con.execute("SELECT url, body FROM documents WHERE url = ANY(?)", (urls,))}


def search_local(
    con: Conn, q: str, limit: int = 20, offset: int = 0, lang: str | None = None, query_lang: str | None = None
) -> list[dict]:
    """Full-text candidates -> rerank (``scoring.score_candidates``) -> diversify by domain.

    ``lang`` filters documents by their language. ``query_lang`` only says which language the *query* is in (e.g. the
    UI language): for Afaan Oromoo queries every term also matches its stem, so ``barataa`` finds ``barattoota``."""
    om = looks_oromoo(q, query_lang or lang)
    build = fts_query_om if om else fts_query
    match = build(q, "and")
    if not match:
        return []
    candidates = 150
    rows = _run(con, match, candidates, lang)
    if len(rows) < 5:  # widen recall with OR when AND is too strict
        seen = {r["url"] for r in rows}
        rows += [r for r in _run(con, build(q, "or"), candidates, lang) if r["url"] not in seen]

    q_toks = content_tokens(q)
    scored = score_candidates(rows, q, time.time(), use_stems=om)
    scored = diversify(scored, per_domain=2, window=10)
    page = scored[offset : offset + limit]
    bodies = _bodies(con, [it["url"] for it in page])  # only the visible page needs its text (for the snippet)
    for it in page:
        it["snippet"] = make_snippet(bodies.get(it["url"], ""), q_toks)
    return page


def stats(con: Conn) -> dict:
    one = lambda sql, args=(): con.execute(sql, args).fetchone()[0]  # noqa: E731
    return {
        "documents": one("SELECT COUNT(*) FROM documents"),
        "domains": one("SELECT COUNT(DISTINCT domain) FROM documents"),
        "frontier_pending": one("SELECT COUNT(*) FROM frontier WHERE status='pending'"),
        "frontier_done": one("SELECT COUNT(*) FROM frontier WHERE status='done'"),
        "frontier_failed": one("SELECT COUNT(*) FROM frontier WHERE status='failed'"),
        "frontier_blocked": one("SELECT COUNT(*) FROM frontier WHERE status='blocked'"),
        "frontier_duplicate": one("SELECT COUNT(*) FROM frontier WHERE status='duplicate'"),
        "frontier_gone": one("SELECT COUNT(*) FROM frontier WHERE status='gone'"),
        "frontier_retrying": one("SELECT COUNT(*) FROM frontier WHERE status='pending' AND attempts > 0"),
        "documents_due_recrawl": one(
            "SELECT COUNT(*) FROM documents WHERE next_crawl_at IS NOT NULL AND next_crawl_at <= ?", (time.time(),)
        ),
        "queries_logged": one("SELECT COUNT(*) FROM query_log"),
        "users": one("SELECT COUNT(*) FROM users"),
    }
