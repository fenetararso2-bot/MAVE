"""Accounts, rotating sessions, password reset, verification, preferences, deletion, migrations, mailer.
Stdlib only: runs without FastAPI/httpx."""
import logging
import os
import threading
import unittest
from unittest import mock

import psycopg

from app import auth, mailer
from app.core import security
from app.auth import AuthError
from app.core.config import settings
import app.db as dbmod
from app.db import MIGRATIONS, SCHEMA_VERSION, db, init_db, schema_version
from tests.pgtest import PgCase, create_database, drop_database

T0 = 1_800_000_000.0  # fixed clock for time-travel tests
DAY = 86400


class AuthCase(PgCase):  # self.dsn: a private, fully migrated PostgreSQL database per test

    def signup(self, email="user@example.com", password="correct-horse-1", now=T0):
        with db(self.dsn) as con:
            pair, vtoken = auth.register(con, email, password, now=now)
        return pair, vtoken

    def revoked_at(self, sid):
        with db(self.dsn) as con:
            return con.execute("SELECT revoked_at FROM sessions WHERE id=?", (sid,)).fetchone()[0]


class TestMigrations(AuthCase):
    def test_fresh_database_is_at_latest_version_and_idempotent(self):
        init_db(self.dsn)
        with db(self.dsn) as con:
            self.assertEqual(schema_version(con), SCHEMA_VERSION)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)
            tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        self.assertTrue({"sessions", "password_resets", "email_verifications", "user_preferences"} <= tables)

    def test_database_at_version_1_is_upgraded_without_losing_users(self):
        legacy = create_database(migrated=False)
        self.addCleanup(drop_database, legacy)
        with mock.patch.object(dbmod, "MIGRATIONS", {1: MIGRATIONS[1]}):  # exactly the first MAVE schema
            init_db(legacy)
        with db(legacy) as con:
            self.assertEqual(schema_version(con), 1)
            con.execute("INSERT INTO users(email, salt, pw_hash, created_at) VALUES('old@example.com', '\\x01'::bytea, '\\x02'::bytea, 1)")
        init_db(legacy)
        with db(legacy) as con:
            self.assertEqual(schema_version(con), SCHEMA_VERSION)
            self.assertEqual([tuple(r) for r in con.execute("SELECT email, email_verified FROM users")], [("old@example.com", 0)])

    def test_failed_migration_rolls_back_and_keeps_version(self):
        p = create_database(migrated=False)
        self.addCleanup(drop_database, p)
        with mock.patch.object(dbmod, "MIGRATIONS", {1: MIGRATIONS[1], 2: MIGRATIONS[2]}):
            init_db(p)
        bad = {**MIGRATIONS, 3: ["CREATE TABLE ok_table(x INTEGER)", "THIS IS NOT SQL"]}
        with mock.patch.object(dbmod, "MIGRATIONS", bad):
            with self.assertRaises(psycopg.Error):
                init_db(p)
        with db(p) as con:
            self.assertEqual(schema_version(con), 2)
            self.assertIsNone(con.execute("SELECT to_regclass('ok_table')").fetchone()[0])  # DDL was rolled back too

    def test_concurrent_workers_apply_each_migration_once(self):
        p = create_database(migrated=False)
        self.addCleanup(drop_database, p)
        errors = []

        def worker():
            try:
                init_db(p)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        with db(p) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)


class TestRegisterLogin(AuthCase):
    def test_register_returns_token_pair_bound_to_a_session(self):
        pair, vtoken = self.signup("  User@Example.COM ")
        self.assertEqual(pair["email"], "user@example.com")
        self.assertFalse(pair["email_verified"])
        self.assertEqual(pair["expires_in"], settings.access_ttl)
        claims = security.verify_token(pair["token"])
        self.assertEqual(claims["typ"], "access")
        self.assertEqual(claims["email"], "user@example.com")
        with db(self.dsn) as con:
            self.assertTrue(auth.session_active(con, claims["sid"], claims["sub"]))
        self.assertGreater(len(vtoken), 20)

    def test_refresh_token_and_reset_token_are_stored_hashed(self):
        pair, vtoken = self.signup()
        with db(self.dsn) as con:
            stored = con.execute("SELECT refresh_hash FROM sessions").fetchone()[0]
            vstored = con.execute("SELECT token_hash FROM email_verifications").fetchone()[0]
        self.assertNotEqual(stored, pair["refresh_token"])
        self.assertEqual(len(stored), 64)
        self.assertNotEqual(vstored, vtoken)

    def test_validation_and_duplicates(self):
        self.signup()
        for email, pw, status in [
            ("user@example.com", "another-pass-1", 409),
            ("USER@example.com", "another-pass-1", 409),
            ("not-an-email", "another-pass-1", 400),
            ("new@example.com", "short", 400),
            ("new@example.com", "x" * 129, 400),
            ("new@example.com", "NEW@example.com", 400),
            ("new@example.com", "new", 400),
        ]:
            with self.subTest(email=email, pw=pw), db(self.dsn) as con, self.assertRaises(AuthError) as cm:
                auth.register(con, email, pw)
            self.assertEqual(cm.exception.status, status)

    def test_login_success_and_failures(self):
        self.signup()
        with db(self.dsn) as con:
            pair = auth.login(con, " USER@example.com ", "correct-horse-1", now=T0 + 5)
        self.assertTrue(pair["refresh_token"])
        for email, pw in [("user@example.com", "wrong-password"), ("ghost@example.com", "correct-horse-1")]:
            with self.subTest(email=email), db(self.dsn) as con, self.assertRaises(AuthError) as cm:
                auth.login(con, email, pw)
            self.assertEqual((cm.exception.status, cm.exception.detail), (401, "Wrong email or password"))

    def test_unknown_email_still_spends_a_password_hash(self):
        with mock.patch.object(auth, "check_password", return_value=False) as chk:
            with db(self.dsn) as con, self.assertRaises(AuthError):
                auth.login(con, "ghost@example.com", "whatever-123")
        self.assertEqual(chk.call_count, 1)  # equalised timing: no early return for unknown accounts

    def test_each_login_is_its_own_session(self):
        self.signup()
        with db(self.dsn) as con:
            a = auth.login(con, "user@example.com", "correct-horse-1")
            b = auth.login(con, "user@example.com", "correct-horse-1")
        self.assertNotEqual(security.verify_token(a["token"])["sid"], security.verify_token(b["token"])["sid"])


class TestSessions(AuthCase):
    def test_refresh_rotates_and_keeps_the_session(self):
        pair, _ = self.signup()
        sid = security.verify_token(pair["token"])["sid"]
        with db(self.dsn) as con:
            new = auth.refresh(con, pair["refresh_token"], now=T0 + 600)
        self.assertNotEqual(new["refresh_token"], pair["refresh_token"])
        self.assertEqual(security.verify_token(new["token"])["sid"], sid)
        with db(self.dsn) as con:  # the new token works, the old one is now a reuse signal
            auth.refresh(con, new["refresh_token"], now=T0 + 700)

    def test_reuse_of_a_rotated_token_revokes_the_session_even_though_the_request_fails(self):
        pair, _ = self.signup()
        sid = security.verify_token(pair["token"])["sid"]
        with db(self.dsn) as con:
            newer = auth.refresh(con, pair["refresh_token"], now=T0 + 10)
        with self.assertRaises(AuthError) as cm:  # attacker (or buggy client) replays the old token
            with db(self.dsn) as con:  # db() rolls back on exceptions: the revoke must survive that
                auth.refresh(con, pair["refresh_token"], now=T0 + 20)
        self.assertEqual(cm.exception.detail, "Session revoked")
        self.assertIsNotNone(self.revoked_at(sid))
        with self.assertRaises(AuthError), db(self.dsn) as con:  # the legitimate newer token died with it
            auth.refresh(con, newer["refresh_token"], now=T0 + 30)
        with db(self.dsn) as con:
            self.assertFalse(auth.session_active(con, sid, security.verify_token(pair["token"])["sub"], now=T0 + 30))

    def test_garbage_and_unknown_refresh_tokens(self):
        for tok in ("", "x", "A" * 43, None):
            with self.subTest(tok=tok), db(self.dsn) as con, self.assertRaises(AuthError) as cm:
                auth.refresh(con, tok)
            self.assertEqual(cm.exception.status, 401)

    def test_sliding_expiry_and_absolute_cap(self):
        pair, _ = self.signup()
        tok = pair["refresh_token"]
        for days in (20, 40, 60, 80):  # each refresh within 30 days of the previous one keeps the session alive
            with db(self.dsn) as con:
                tok = auth.refresh(con, tok, now=T0 + days * DAY)["refresh_token"]
        with self.assertRaises(AuthError) as cm, db(self.dsn) as con:  # day 95 > 90-day hard cap
            auth.refresh(con, tok, now=T0 + 95 * DAY)
        self.assertEqual(cm.exception.detail, "Session expired")

    def test_active_check_honours_the_absolute_cap_so_old_access_tokens_die_with_the_session(self):
        pair, _ = self.signup()
        claims = security.verify_token(pair["token"])
        tok = pair["refresh_token"]
        for days in (20, 40, 60, 80):
            with db(self.dsn) as con:
                tok = auth.refresh(con, tok, now=T0 + days * DAY)["refresh_token"]
        with db(self.dsn) as con:
            self.assertTrue(auth.session_active(con, claims["sid"], claims["sub"], now=T0 + 89 * DAY))
            # day 80 + 30-day slide would reach day 110, but the session may not outlive day 90
            self.assertFalse(auth.session_active(con, claims["sid"], claims["sub"], now=T0 + 91 * DAY))

    def test_idle_session_expires(self):
        pair, _ = self.signup()
        with self.assertRaises(AuthError) as cm, db(self.dsn) as con:
            auth.refresh(con, pair["refresh_token"], now=T0 + 31 * DAY)
        self.assertEqual(cm.exception.detail, "Session expired")

    def test_logout_by_session_is_immediate_and_idempotent(self):
        pair, _ = self.signup()
        claims = security.verify_token(pair["token"])
        with db(self.dsn) as con:
            auth.logout(con, sid=claims["sid"])
            auth.logout(con, sid=claims["sid"])
            auth.logout(con, sid="does-not-exist")
            self.assertFalse(auth.session_active(con, claims["sid"], claims["sub"]))
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.refresh(con, pair["refresh_token"])

    def test_logout_by_refresh_token_needs_no_access_token(self):
        pair, _ = self.signup()
        with db(self.dsn) as con:
            auth.logout(con, refresh_token=pair["refresh_token"])
            auth.logout(con, refresh_token="never-issued")
        self.assertIsNotNone(self.revoked_at(security.verify_token(pair["token"])["sid"]))

    def test_logout_everywhere_only_touches_that_user(self):
        a1, _ = self.signup("a@example.com")
        with db(self.dsn) as con:
            a2 = auth.login(con, "a@example.com", "correct-horse-1")
        b1, _ = self.signup("b@example.com")
        uid = security.verify_token(a1["token"])["sub"]
        with db(self.dsn) as con:
            auth.logout(con, user_id=uid, everywhere=True)
        self.assertIsNotNone(self.revoked_at(security.verify_token(a1["token"])["sid"]))
        self.assertIsNotNone(self.revoked_at(security.verify_token(a2["token"])["sid"]))
        self.assertIsNone(self.revoked_at(security.verify_token(b1["token"])["sid"]))
        with self.assertRaises(AuthError) as cm, db(self.dsn) as con:
            auth.logout(con, everywhere=True)
        self.assertEqual(cm.exception.status, 401)

    def test_session_of_another_user_is_not_active(self):
        a, _ = self.signup("a@example.com")
        b, _ = self.signup("b@example.com")
        with db(self.dsn) as con:
            self.assertFalse(
                auth.session_active(con, security.verify_token(a["token"])["sid"], security.verify_token(b["token"])["sub"])
            )

    def test_access_token_header_must_be_hs256_and_expiry_is_enforced(self):
        header = security._b64(b'{"alg":"HS512","typ":"JWT"}')
        payload = security._b64(b'{"sub":1,"exp":9999999999}')
        forged = f"{header}.{payload}.{security._sign(header + '.' + payload, settings.jwt_secret)}"
        self.assertIsNone(security.verify_token(forged))
        self.assertIsNone(security.verify_token(security.make_token(1, "a@b.co", ttl=-5)))


class TestPasswordReset(AuthCase):
    def test_unknown_email_yields_nothing(self):
        with db(self.dsn) as con:
            self.assertIsNone(auth.request_password_reset(con, "ghost@example.com", now=T0))

    def test_full_reset_flow_changes_password_and_ends_all_sessions(self):
        pair, _ = self.signup()
        sid = security.verify_token(pair["token"])["sid"]
        with db(self.dsn) as con:
            email, token = auth.request_password_reset(con, " USER@example.com ", now=T0 + 100)
        self.assertEqual(email, "user@example.com")
        with db(self.dsn) as con:
            auth.reset_password(con, token, "brand-new-pass-9", now=T0 + 200)
        self.assertIsNotNone(self.revoked_at(sid))
        with db(self.dsn) as con:
            with self.assertRaises(AuthError):
                auth.login(con, "user@example.com", "correct-horse-1")
            login = auth.login(con, "user@example.com", "brand-new-pass-9")
            self.assertTrue(login["email_verified"])  # reading the reset mail proves the inbox is theirs

    def test_reset_token_is_single_use_and_expires(self):
        self.signup()
        with db(self.dsn) as con:
            _, token = auth.request_password_reset(con, "user@example.com", now=T0)
        with db(self.dsn) as con:
            auth.reset_password(con, token, "brand-new-pass-9", now=T0 + 10)
        with self.assertRaises(AuthError) as cm, db(self.dsn) as con:
            auth.reset_password(con, token, "another-pass-77", now=T0 + 20)
        self.assertEqual(cm.exception.status, 400)
        with db(self.dsn) as con:
            _, token2 = auth.request_password_reset(con, "user@example.com", now=T0 + 1000)
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.reset_password(con, token2, "yet-another-pw-5", now=T0 + 1000 + auth.RESET_TTL + 1)

    def test_weak_password_does_not_burn_the_token(self):
        self.signup()
        with db(self.dsn) as con:
            _, token = auth.request_password_reset(con, "user@example.com", now=T0)
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.reset_password(con, token, "short", now=T0 + 5)
        with db(self.dsn) as con:
            auth.reset_password(con, token, "long-enough-pass-1", now=T0 + 6)

    def test_invalid_tokens_are_rejected(self):
        for tok in ("", "nope", None):
            with self.subTest(tok=tok), self.assertRaises(AuthError) as cm, db(self.dsn) as con:
                auth.reset_password(con, tok, "long-enough-pass-1")
            self.assertEqual(cm.exception.status, 400)

    def test_mail_bombing_is_throttled_and_new_request_replaces_old_token(self):
        self.signup()
        with db(self.dsn) as con:
            _, first = auth.request_password_reset(con, "user@example.com", now=T0)
            self.assertIsNone(auth.request_password_reset(con, "user@example.com", now=T0 + 30))
            _, second = auth.request_password_reset(con, "user@example.com", now=T0 + 61)
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.reset_password(con, first, "long-enough-pass-1", now=T0 + 70)
        with db(self.dsn) as con:
            auth.reset_password(con, second, "long-enough-pass-1", now=T0 + 70)


class TestEmailVerification(AuthCase):
    def test_verify_flow_single_use(self):
        pair, vtoken = self.signup()
        uid = security.verify_token(pair["token"])["sub"]
        with db(self.dsn) as con:
            self.assertFalse(auth.get_profile(con, uid)["email_verified"])
            self.assertEqual(auth.verify_email(con, vtoken, now=T0 + 10), "user@example.com")
            self.assertTrue(auth.get_profile(con, uid)["email_verified"])
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.verify_email(con, vtoken, now=T0 + 20)

    def test_expired_and_garbage_tokens(self):
        _, vtoken = self.signup()
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.verify_email(con, vtoken, now=T0 + auth.VERIFY_TTL + 1)
        with self.assertRaises(AuthError), db(self.dsn) as con:
            auth.verify_email(con, "garbage")

    def test_resend_throttle_and_noop_when_already_verified(self):
        pair, vtoken = self.signup()
        uid = security.verify_token(pair["token"])["sub"]
        with db(self.dsn) as con:
            self.assertIsNone(auth.request_verification(con, uid, now=T0 + 10))  # too soon after signup mail
            email, fresh = auth.request_verification(con, uid, now=T0 + 120)
            self.assertEqual(email, "user@example.com")
            auth.verify_email(con, fresh, now=T0 + 130)
            self.assertIsNone(auth.request_verification(con, uid, now=T0 + 999))
        with self.assertRaises(AuthError), db(self.dsn) as con:  # the signup token was replaced by the resend
            auth.verify_email(con, vtoken, now=T0 + 140)


class TestPreferencesAndDeletion(AuthCase):
    def test_preferences_default_partial_update_and_validation(self):
        pair, _ = self.signup()
        uid = security.verify_token(pair["token"])["sub"]
        with db(self.dsn) as con:
            self.assertEqual(auth.get_preferences(con, uid), {"lang": None, "theme": None})
            self.assertEqual(auth.set_preferences(con, uid, lang="om"), {"lang": "om", "theme": None})
            self.assertEqual(auth.set_preferences(con, uid, theme="dark"), {"lang": "om", "theme": "dark"})
            self.assertEqual(auth.get_profile(con, uid)["preferences"], {"lang": "om", "theme": "dark"})
        for kwargs in ({"lang": "xx"}, {"theme": "neon"}):
            with self.subTest(**kwargs), self.assertRaises(AuthError), db(self.dsn) as con:
                auth.set_preferences(con, uid, **kwargs)

    def _populate(self, uid):
        with db(self.dsn) as con:
            con.execute("INSERT INTO history(user_id, query, ts) VALUES(?, 'q', 1)", (uid,))
            con.execute("INSERT INTO saved(user_id, url, title, ts) VALUES(?, 'https://a.com', 'T', 1)", (uid,))
            auth.set_preferences(con, uid, lang="en")

    def test_delete_account_requires_password_and_removes_everything(self):
        a, _ = self.signup("a@example.com")
        b, _ = self.signup("b@example.com")
        ua, ub = (security.verify_token(x["token"])["sub"] for x in (a, b))
        self._populate(ua)
        self._populate(ub)
        with self.assertRaises(AuthError) as cm, db(self.dsn) as con:
            auth.delete_account(con, ua, "wrong-password-1")
        self.assertEqual(cm.exception.status, 401)
        with db(self.dsn) as con:
            auth.request_password_reset(con, "a@example.com", now=T0 + 500)
            auth.delete_account(con, ua, "correct-horse-1")
        with db(self.dsn) as con:
            for table in ("users", "history", "saved", "sessions", "user_preferences", "password_resets", "email_verifications"):
                col = "id" if table == "users" else "user_id"
                self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (ua,)).fetchone()[0], 0, table)
            for table in ("users", "history", "saved", "sessions", "user_preferences"):  # user b is untouched
                col = "id" if table == "users" else "user_id"
                self.assertGreater(con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (ub,)).fetchone()[0], 0, table)
            with self.assertRaises(AuthError):
                auth.login(con, "a@example.com", "correct-horse-1")

    def test_expired_rows_are_purged_on_login(self):
        self.signup()
        with db(self.dsn) as con:
            auth.login(con, "user@example.com", "correct-horse-1", now=T0 + 200 * DAY)
            n = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(n, 1)  # the day-0 session (expired 170 days ago) was purged, only the new one remains


class TestMailer(unittest.TestCase):
    def test_development_without_smtp_logs_the_mail_and_reports_success(self):
        with mock.patch.dict(os.environ, {"MAVE_ENV": "development"}):
            os.environ.pop("MAVE_SMTP_HOST", None)
            with self.assertLogs("mave.mail", level="INFO") as logs:
                self.assertTrue(mailer.send("u@example.com", "Subj", "code: SECRET-123"))
        self.assertIn("SECRET-123", "\n".join(logs.output))

    def test_production_without_smtp_never_logs_the_secret(self):
        with mock.patch.dict(os.environ, {"MAVE_ENV": "production"}):
            os.environ.pop("MAVE_SMTP_HOST", None)
            with self.assertLogs("mave.mail", level="WARNING") as logs:
                self.assertFalse(mailer.send("user@example.com", "Subj", "code: SECRET-123"))
        text = "\n".join(logs.output)
        self.assertNotIn("SECRET-123", text)
        self.assertNotIn("user@example.com", text)  # address is masked too

    def test_smtp_path_builds_the_message_and_swallows_failures(self):
        env = {"MAVE_SMTP_HOST": "smtp.test", "MAVE_SMTP_FROM": "MAVE <no-reply@mave.test>"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(mailer, "_deliver") as deliver:
                self.assertTrue(mailer.send("u@example.com", "Hello", "body"))
            msg = deliver.call_args.args[0]
            self.assertEqual((msg["To"], msg["Subject"], msg["From"]), ("u@example.com", "Hello", "MAVE <no-reply@mave.test>"))
            with mock.patch.object(mailer, "_deliver", side_effect=OSError("down")):
                with self.assertLogs("mave.mail", level="ERROR"):
                    self.assertFalse(mailer.send("u@example.com", "Hello", "body"))

    def test_email_bodies_contain_the_code_and_link_only_when_public_url_is_set(self):
        with mock.patch.dict(os.environ, {"MAVE_PUBLIC_URL": "https://mave.test/"}):
            _, body = mailer.reset_email("TOK123")
            self.assertIn("https://mave.test/reset-password?token=TOK123", body)
            self.assertIn("TOK123", mailer.verification_email("TOK123")[1])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAVE_PUBLIC_URL", None)
            _, body = mailer.reset_email("TOK123")
            self.assertIn("TOK123", body)
            self.assertNotIn("http", body)


if __name__ == "__main__":
    unittest.main()
