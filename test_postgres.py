"""PostgreSQL-specific behaviour: placeholder translation, full-text search (accents, Afaan Oromoo, prefix),
index usage, crawler claim semantics, text hygiene and the SQLite -> PostgreSQL copy script."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from app.crawler import frontier as fr
from app.crawler import normalize_url
from app.db import Conn, Row, db, to_pyformat
from app.search.engine import add_link_counts, refresh_vocab, search_local, upsert_document
from app.sqlite_import import copy_sqlite
from app.search.suggest import spell_correct
from tests.pgtest import PgCase


def lorem(topic: str, n: int = 30) -> str:
    return (f"{topic} is discussed here in detail. " * n)[:2000]


class TestPlaceholders(unittest.TestCase):
    def test_question_marks_become_format_placeholders_outside_literals(self):
        self.assertEqual(to_pyformat("SELECT * FROM t WHERE a=? AND b='what?' AND c=?"), "SELECT * FROM t WHERE a=%s AND b='what?' AND c=%s")
        self.assertEqual(to_pyformat("x LIKE ? ESCAPE '\\'"), "x LIKE %s ESCAPE '\\'")
        self.assertEqual(to_pyformat("SELECT 100 % 7, ?"), "SELECT 100 %% 7, %s")
        self.assertEqual(to_pyformat("SELECT 'it''s ?', ?"), "SELECT 'it''s ?', %s")

    def test_rows_work_by_name_position_and_dict(self):
        r = Row((1, "a"), {"id": 0, "name": 1})
        self.assertEqual((r["id"], r[1], r[0], dict(r)), (1, "a", 1, {"id": 1, "name": "a"}))


class TestDbLayer(PgCase):
    def test_commit_on_success_rollback_on_error(self):
        with db(self.dsn) as con:
            con.execute("INSERT INTO query_log(query, ts) VALUES(?,?)", ("kept", 1.0))
        with self.assertRaises(RuntimeError):
            with db(self.dsn) as con:
                con.execute("INSERT INTO query_log(query, ts) VALUES(?,?)", ("lost", 2.0))
                raise RuntimeError
        with db(self.dsn) as con:
            self.assertEqual([r["query"] for r in con.execute("SELECT query FROM query_log")], ["kept"])

    def test_nul_characters_are_dropped_instead_of_failing(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Ti\x00tle", lorem("nul\x00byte"))
            row = con.execute("SELECT title, body FROM documents").fetchone()
        self.assertEqual(row["title"], "Title")
        self.assertNotIn("\x00", row["body"])

    def test_history_and_saved_upserts(self):  # the statements used by /me/history and /me/saved
        with db(self.dsn) as con:
            uid = con.execute("INSERT INTO users(email, salt, pw_hash, created_at) VALUES('a@b.co', ?, ?, 1) RETURNING id", (b"s", b"p")).fetchone()[0]
            for ts in (1.0, 2.0):
                con.execute(
                    """INSERT INTO history(user_id, query, ts) VALUES(?,?,?)
                       ON CONFLICT(user_id, query) DO UPDATE SET ts=excluded.ts""", (str(uid), "kotlin", ts))
                con.execute(
                    """INSERT INTO saved(user_id, url, title, snippet, thumbnail, ts) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(user_id, url) DO UPDATE SET title=excluded.title, ts=excluded.ts""",
                    (str(uid), "https://a.com", f"T{ts}", "", None, ts))
            self.assertEqual(con.execute("SELECT query, ts FROM history WHERE user_id=?", (str(uid),)).fetchall()[0]["ts"], 2.0)
            self.assertEqual([dict(r) for r in con.execute("SELECT title, url, snippet, thumbnail FROM saved WHERE user_id=?", (str(uid),))],
                             [{"title": "T2.0", "url": "https://a.com", "snippet": "", "thumbnail": None}])
            con.execute("DELETE FROM history WHERE user_id=? AND query=?", (str(uid), "kotlin"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM history").fetchone()[0], 0)

    def test_a_failed_statement_inside_a_savepoint_does_not_abort_the_transaction(self):
        with db(self.dsn) as con:
            con.execute("INSERT INTO seeds(url, added_at) VALUES('https://a.test/', 1)")
            try:
                with con.transaction():
                    con.execute("INSERT INTO seeds(url, added_at) VALUES('https://a.test/', 2)")
            except Exception:
                pass
            self.assertEqual(con.execute("SELECT COUNT(*) FROM seeds").fetchone()[0], 1)


class TestFullTextSearch(PgCase):
    def test_accents_are_ignored_both_ways(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Café Müller", lorem("crème brûlée"))
            self.assertEqual(len(search_local(con, "cafe muller")), 1)
            self.assertEqual(len(search_local(con, "CRÈME")), 1)
            self.assertEqual(len(search_local(con, "brulee")), 1)

    def test_afaan_oromoo_text_with_apostrophes_and_prefixes(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://om.example/1", "Afaan Oromoo barnoota", lorem("barumsa afaan oromoo ta'e"), "om")
            upsert_document(con, "https://om.example/2", "Oromiyaa", lorem("magaalaa finfinnee"), "om")
            self.assertEqual(search_local(con, "afaan orom")[0]["url"], "https://om.example/1")  # AND hit first, OR fallback after
            self.assertEqual(len(search_local(con, "ta'e")), 1)
            self.assertEqual(len(search_local(con, "finfinn")), 1)
            self.assertEqual(len(search_local(con, "oromoo", lang="om")), 1)
            self.assertEqual(search_local(con, "oromoo", lang="en"), [])

    def test_hostile_queries_are_harmless(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Kotlin guide", lorem("kotlin"))
            for q in ["'", "\\", "a' | 'b", "!!!", "kotlin & !", "( kotlin", "kotlin:*", "<->", "x" * 2000, "\"quoted\" 'single'", "_ _"]:
                search_local(con, q)  # must not raise
            self.assertEqual(con.execute("SELECT 1").fetchone()[0], 1)  # transaction still healthy

    def test_title_outranks_body_and_snippet_comes_from_the_body(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Kotlin coroutines", "intro " * 40 + "coroutines are lightweight threads " + "x " * 40)
            upsert_document(con, "https://b.com/1", "Gardening", "kotlin coroutines " * 3 + "tomatoes " * 40)
            res = search_local(con, "kotlin coroutines")
        self.assertEqual(res[0]["url"], "https://a.com/1")
        self.assertIn("coroutines", res[0]["snippet"])

    def test_the_gin_index_is_used(self):
        with db(self.dsn) as con:
            for i in range(300):
                upsert_document(con, f"https://s{i}.com/", f"Page {i}", f"filler words number {i} " * 20)
            upsert_document(con, "https://rare.com/", "Needle", lorem("zzzneedle"))
            con.execute("ANALYZE documents")
            con.execute("SET LOCAL enable_seqscan = off")  # tiny table: force the planner to show what it *can* use
            plan = " ".join(r[0] for r in con.execute(
                "EXPLAIN SELECT id FROM documents WHERE tsv @@ to_tsquery('simple', mave_unaccent('zzzneedle'))"))
        self.assertIn("idx_docs_tsv", plan)

    def test_spell_correct_uses_accent_stripped_vocabulary_and_live_check(self):
        with db(self.dsn) as con:
            upsert_document(con, "https://a.com/1", "Technology café", lorem("technology"))
            refresh_vocab(con)
            self.assertEqual(spell_correct(con, "tecnology cafe"), "technology cafe")
            upsert_document(con, "https://a.com/2", "Quantum", lorem("quantum"))  # newer than the vocabulary
            self.assertIsNone(spell_correct(con, "quantum"))  # known word: never "corrected"
            self.assertIsNone(spell_correct(con, "café"))


class TestCrawlerStorage(PgCase):
    def test_claim_next_never_hands_the_same_url_to_two_workers(self):
        with db(self.dsn) as con:
            for i in range(40):
                fr.add(con, f"https://s.test/{i}", 0, 1.0, 1.0)
        claimed, lock = [], threading.Lock()

        def worker():
            while True:
                with db(self.dsn) as con:
                    row = fr.claim_next(con, time.time())
                if row is None:
                    return
                with lock:
                    claimed.append(row["url"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(claimed), 40)
        self.assertEqual(len(set(claimed)), 40)

    def test_concurrent_link_counting_does_not_deadlock(self):
        urls = [f"https://l.test/{i}" for i in range(50)]
        errors = []

        def worker(seed):
            try:
                for _ in range(5):
                    with db(self.dsn) as con:
                        add_link_counts(con, urls[seed:] + urls[:seed])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i * 10,)) for i in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        with db(self.dsn) as con:
            self.assertEqual(con.execute("SELECT MIN(n), MAX(n) FROM link_counts").fetchone()[0], 25)

    def test_over_long_urls_are_not_crawlable(self):
        self.assertIsNone(normalize_url("https://a.com/" + "x" * 3000))
        self.assertIsNone(normalize_url("https://a.com/" + "é" * 1500))  # 3000 bytes
        self.assertIsNotNone(normalize_url("https://a.com/" + "x" * 1900))


# The schema of the last SQLite-based MAVE (v0.9), kept here only to build an "old database" for the import test.
OLD_SQLITE = """
CREATE TABLE users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, pw_hash BLOB NOT NULL,
  created_at REAL NOT NULL, email_verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE history(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, query TEXT NOT NULL, ts REAL NOT NULL, UNIQUE(user_id, query));
CREATE TABLE saved(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
  snippet TEXT NOT NULL DEFAULT '', thumbnail TEXT, ts REAL NOT NULL, UNIQUE(user_id, url));
CREATE TABLE documents(id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, domain TEXT NOT NULL, title TEXT NOT NULL,
  body TEXT NOT NULL, lang TEXT, fetched_at REAL NOT NULL, canonical_url TEXT, content_hash TEXT, simhash INTEGER,
  crawl_interval REAL, next_crawl_at REAL);
CREATE TABLE link_counts(url TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0);
CREATE TABLE frontier(url TEXT PRIMARY KEY, depth INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  added_at REAL NOT NULL, priority REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE simhash_bands(band INTEGER NOT NULL, value INTEGER NOT NULL, doc_id INTEGER NOT NULL);
CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, content='documents', content_rowid='id');
"""


class TestSqliteImport(PgCase):
    def test_old_database_is_copied_with_ids_sequences_and_search(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        src = sqlite3.connect(path)
        src.executescript(OLD_SQLITE)
        src.execute("INSERT INTO users VALUES(7, 'a@b.co', x'01', x'02', 1.0, 1)")
        src.execute("INSERT INTO history VALUES(1, 7, 'kotlin', 5.0)")
        src.execute("INSERT INTO history VALUES(2, 99, 'orphan', 5.0)")  # user 99 does not exist
        src.execute("INSERT INTO documents(id,url,domain,title,body,lang,fetched_at,simhash) VALUES(3,'https://a.com/','a.com','Kotlin café',?,'en',1.0,-5)", ("body\x00 text kotlin",))
        src.execute("INSERT INTO documents(id,url,domain,title,body,fetched_at) VALUES(4,?, 'a.com','Long','x',1.0)", ("https://a.com/" + "u" * 3000,))
        src.execute("INSERT INTO link_counts VALUES('https://a.com/', 4)")
        src.execute("INSERT INTO frontier(url,depth,status,added_at) VALUES('https://a.com/',0,'done',1.0)")
        src.execute("INSERT INTO simhash_bands VALUES(0, 12, 3)")
        src.commit()
        src.close()

        report = copy_sqlite(path, self.dsn, log=lambda *_: None)
        self.assertEqual(report["history"], {"copied": 1, "skipped": 1})
        self.assertEqual(report["documents"], {"copied": 1, "skipped": 1})
        with db(self.dsn) as con:
            self.assertEqual(con.execute("SELECT email, email_verified FROM users").fetchone()[:], ("a@b.co", 1))
            self.assertEqual(bytes(con.execute("SELECT salt FROM users").fetchone()[0]), b"\x01")
            self.assertEqual(len(search_local(con, "kotlin cafe")), 1)  # tsv was built by PostgreSQL
            self.assertEqual(con.execute("SELECT simhash FROM documents").fetchone()[0], -5)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM docs_vocab WHERE term='kotlin'").fetchone()[0], 1)
            # sequences continue after the imported ids
            self.assertEqual(con.execute("INSERT INTO users(email, salt, pw_hash, created_at) VALUES('n@b.co', ?, ?, 2) RETURNING id", (b"s", b"p")).fetchone()[0], 8)
            self.assertEqual(con.execute("INSERT INTO documents(url,domain,title,body,fetched_at) VALUES('https://n.com/','n.com','t','b',1) RETURNING id").fetchone()[0], 4)  # id 4 was the skipped over-long URL

    def test_refuses_a_target_that_already_has_data(self):
        with db(self.dsn) as con:
            con.execute("INSERT INTO seeds(url, added_at) VALUES('https://x.test/', 1)")
            con.execute("INSERT INTO users(email, salt, pw_hash, created_at) VALUES('x@y.zz', ?, ?, 1)", (b"s", b"p"))
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        sqlite3.connect(path).executescript(OLD_SQLITE)
        with self.assertRaises(SystemExit):
            copy_sqlite(path, self.dsn, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
