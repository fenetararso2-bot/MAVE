"""CLI: python migrate_sqlite.py mave.db [--url postgresql://user:pass@host/db]

Copies an existing SQLite MAVE database into PostgreSQL (default target: MAVE_DATABASE_URL). The target database
must exist and be empty; the schema is created automatically.
"""
import argparse

from app.core.config import settings
from app.db import redact_dsn
from app.sqlite_import import copy_sqlite

ap = argparse.ArgumentParser()
ap.add_argument("sqlite_file", help="path of the old mave.db")
ap.add_argument("--url", default=None, help="PostgreSQL URL (default: MAVE_DATABASE_URL)")
ap.add_argument("--batch", type=int, default=500)
args = ap.parse_args()

target = args.url or settings.database_url
print(f"Importing {args.sqlite_file} -> {redact_dsn(target)}")
copy_sqlite(args.sqlite_file, target, batch=args.batch)
print("Done.")
