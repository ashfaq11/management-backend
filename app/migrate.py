"""Tiny forward-only migrations: migrations/NNN_name.sql, each applied once, in order.

Runs under an advisory lock, so several API processes starting together cannot race.
"""
import pathlib

import psycopg
from psycopg import sql

DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
LOCK_KEY = 7_411_902  # arbitrary app-wide constant


def migrate(dsn: str, schema: str) -> list[str]:
    applied = []
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))  # released when the connection closes
        c.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        c.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        c.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
                       version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
        for f in sorted(DIR.glob("*.sql")):
            if f.name in done:
                continue
            with c.transaction():
                c.execute(f.read_text())
                c.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied
