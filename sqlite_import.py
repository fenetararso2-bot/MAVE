"""One-off import of a MAVE SQLite database (the pre-PostgreSQL ``mave.db``) into PostgreSQL.

Used by ``python migrate_sqlite.py mave.db``. The target must be empty. Foreign-key orphans (SQLite did not enforce
foreign keys on every connection) and rows whose URL is too long for a PostgreSQL index are skipped and counted;
NUL characters are removed from text. The full-text column and vocabulary are rebuilt by PostgreSQL itself.
"""
import sqlite3

from .db import db, init_db
from .search.engine import refresh_vocab

# parents first. (table, parent filter that drops orphans)
TABLES: list[tuple[str, str | None]] = [
    ("users", None),
    ("documents", None),
    ("history", "user_id IN (SELECT id FROM users)"),
    ("saved", "user_id IN (SELECT id FROM users)"),
    ("sessions", "user_id IN (SELECT id FROM users)"),
    ("password_resets", "user_id IN (SELECT id FROM users)"),
    ("email_verifications", "user_id IN (SELECT id FROM users)"),
    ("user_preferences", "user_id IN (SELECT id FROM users)"),
    ("query_log", None),
    ("link_counts", None),
    ("frontier", None),
    ("simhash_bands", "doc_id IN (SELECT id FROM documents)"),
    ("seeds", None),
    ("audit_log", None),
]
IDENTITY_TABLES = ("users", "history", "saved", "query_log", "documents", "audit_log")
MAX_KEY_BYTES = 2000


def _clean(v):
    return v.replace("\x00", "") if isinstance(v, str) else v


def copy_sqlite(sqlite_path: str, dsn: str | None = None, batch: int = 500, log=print) -> dict[str, dict]:
    """Copy every table. Returns ``{table: {"copied": n, "skipped": m}}``."""
    init_db(dsn)  # make sure the PostgreSQL schema exists and is current
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    report: dict[str, dict] = {}
    try:
        have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with db(dsn) as con:
            for table, _ in TABLES:
                if con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise SystemExit(f"Refusing to import: PostgreSQL table '{table}' is not empty.")
            for table, orphan_filter in TABLES:
                if table not in have:
                    continue
                src_cols = [r["name"] for r in src.execute(f"PRAGMA table_info({table})")]
                pg_cols = {
                    r[0]
                    for r in con.execute(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema='public' AND table_name=? AND is_generated='NEVER'", (table,)
                    )
                }
                cols = [c for c in src_cols if c in pg_cols]
                total = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                where = f" WHERE {orphan_filter}" if orphan_filter else ""
                insert = f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})"
                copied, rows = 0, []
                for row in src.execute(f"SELECT {', '.join(cols)} FROM {table}{where}"):
                    values = [_clean(row[c]) for c in cols]
                    if any(c in ("url", "doc_url") and isinstance(v, str) and len(v.encode("utf-8", "ignore")) > MAX_KEY_BYTES
                           for c, v in zip(cols, values)):
                        continue
                    rows.append(values)
                    if len(rows) >= batch:
                        con.executemany(insert, rows)
                        copied += len(rows)
                        rows = []
                if rows:
                    con.executemany(insert, rows)
                    copied += len(rows)
                report[table] = {"copied": copied, "skipped": total - copied}
                log(f"{table}: {copied} copied" + (f", {total - copied} skipped" if total != copied else ""))
            for table in IDENTITY_TABLES:  # explicit ids were inserted: move the sequences past them
                con.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
            refresh_vocab(con)
    finally:
        src.close()
    return report
