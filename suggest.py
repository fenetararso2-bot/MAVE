"""Autocomplete, trending queries and spelling correction."""
import time

from ..db import Conn
from ..core.textutil import levenshtein, strip_accents, tokens, ts_term

DEFAULT_TRENDING = ["Artificial Intelligence", "Afaan Oromoo", "Technology news", "Startups", "Kotlin"]


def log_query(con: Conn, q: str) -> None:
    q = " ".join(q.split())[:200]
    if q:
        con.execute("INSERT INTO query_log(query, ts) VALUES(?,?)", (q.lower(), time.time()))


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def suggest(con: Conn, prefix: str, limit: int = 8) -> list[str]:
    prefix = " ".join(prefix.lower().split())
    if not prefix:
        return []
    like = _like_escape(prefix) + "%"
    out: list[str] = []
    for r in con.execute(
        """SELECT query, COUNT(*) c FROM query_log WHERE query LIKE ? ESCAPE '\\'
           GROUP BY query ORDER BY c DESC, MAX(ts) DESC LIMIT ?""",
        (like, limit),
    ):
        out.append(r["query"])
    if len(out) < limit:
        for r in con.execute(
            "SELECT title FROM documents WHERE lower(title) LIKE ? ESCAPE '\\' LIMIT ?", (like, limit)
        ):
            t = r["title"].strip()
            if t.lower() not in {o.lower() for o in out}:
                out.append(t)
            if len(out) >= limit:
                break
    return out[:limit]


def trending(con: Conn, days: int = 7, limit: int = 8) -> list[str]:
    since = time.time() - days * 86400
    rows = con.execute(
        "SELECT query, COUNT(*) c FROM query_log WHERE ts >= ? GROUP BY query ORDER BY c DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    found = [r["query"] for r in rows]
    for d in DEFAULT_TRENDING:
        if len(found) >= limit:
            break
        if d.lower() not in found:
            found.append(d)
    return found[:limit]


def spell_correct(con: Conn, q: str) -> str | None:
    """Return a corrected query using the index vocabulary, or None if nothing to fix."""
    toks = tokens(q)
    if not toks:
        return None
    fixed, changed = [], False
    for t in toks:
        if len(t) < 4 or t.isdigit():
            fixed.append(t)
            continue
        plain = strip_accents(t)  # the index stores accent-stripped terms
        # exact check against the live index (a GIN lookup), so words indexed after the last vocabulary
        # refresh are never "corrected" into a different word
        if con.execute(
            "SELECT 1 FROM documents WHERE tsv @@ to_tsquery('simple', mave_unaccent(?)) LIMIT 1", (ts_term(t),)
        ).fetchone():
            fixed.append(t)
            continue
        cands = con.execute(
            "SELECT term, cnt FROM docs_vocab WHERE term LIKE ? ESCAPE '\\' AND length(term) BETWEEN ? AND ?",
            (_like_escape(plain[0]) + "%", len(plain) - 2, len(plain) + 2),
        ).fetchall()
        best, best_key = None, None
        for c in cands:
            d = levenshtein(plain, c["term"], limit=2)
            if d <= 2:
                key = (d, -c["cnt"])
                if best_key is None or key < best_key:
                    best, best_key = c["term"], key
        if best:
            fixed.append(best)
            changed = True
        else:
            fixed.append(t)
    return " ".join(fixed) if changed else None
