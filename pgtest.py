"""PostgreSQL fixtures for the test-suite.

The tests need a running PostgreSQL server (>= 13) and a role that may CREATE DATABASE. Point them at it with

    MAVE_TEST_DATABASE_URL=postgresql://mave:mave@localhost:5432/postgres      (this is the default)

Every test gets its own throw-away database, cloned from a template that is migrated once per run, so tests are
isolated from each other and from your real data (nothing but databases named ``mave_test_*`` is touched).
"""
import atexit
import os
import secrets
import unittest

import psycopg
from psycopg.conninfo import make_conninfo

from app import db as dbmod

_template: str | None = None


def admin_dsn() -> str:
    return os.environ.get("MAVE_TEST_DATABASE_URL", "postgresql://mave:mave@localhost:5432/postgres")


def _exec(sql: str) -> None:
    with psycopg.connect(admin_dsn(), autocommit=True) as con:
        con.execute(sql)


def _dsn_for(name: str) -> str:
    return make_conninfo(admin_dsn(), dbname=name)


def _template_name() -> str:
    global _template
    if _template is None:
        name = "mave_test_tpl_" + secrets.token_hex(4)
        _exec(f'CREATE DATABASE "{name}"')
        dbmod.init_db(_dsn_for(name))
        atexit.register(lambda: _exec(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        _template = name
    return _template


def create_database(migrated: bool = True) -> str:
    """A fresh database; ``migrated=False`` gives an empty one (for migration tests). Returns its DSN."""
    name = "mave_test_" + secrets.token_hex(6)
    _exec(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{_template_name()}"' if migrated else ""))
    return _dsn_for(name)


def drop_database(dsn: str) -> None:
    dbmod.close_pool(dsn)
    name = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
    assert name.startswith("mave_test_"), name  # never drop anything that is not a test database
    _exec(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


class PgCase(unittest.TestCase):
    """``self.dsn`` is a private, fully migrated database for each test."""

    def setUp(self):
        self.dsn = create_database()
        self.addCleanup(drop_database, self.dsn)
