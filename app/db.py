import os
import re
from contextlib import contextmanager

import psycopg
from psycopg.adapt import Loader
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .migrate import migrate

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_TZ = re.compile(r"^[A-Za-z0-9_/+\-]{1,64}$")


# The JSON API speaks floats and ISO strings; make the driver return those instead of Decimal/datetime.
class _Float(Loader):
    def load(self, data):
        return float(bytes(data))


class _Text(Loader):
    def load(self, data):
        return bytes(data).decode()


class _Local(Loader):  # timestamp (spa wall clock): "YYYY-MM-DDTHH:MM"
    def load(self, data):
        return bytes(data).decode().replace(" ", "T")[:16]


class _Instant(Loader):  # timestamptz, rendered in the session zone: "YYYY-MM-DDTHH:MM:SS"
    def load(self, data):
        return bytes(data).decode().replace(" ", "T")[:19]


for _oid, _cls in (("numeric", _Float), ("date", _Text), ("timestamp", _Local), ("timestamptz", _Instant)):
    psycopg.adapters.register_loader(_oid, _cls)


class Tx:
    """Statements on one connection. Used directly inside `Database.tx()`."""

    def __init__(self, conn):
        self.conn = conn

    def all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql, args=()) -> int:
        return self.conn.execute(sql, args).rowcount

    def insert(self, sql, args=()) -> int:
        return self.conn.execute(sql + " RETURNING id", args).fetchone()["id"]


class Database(Tx):
    """Connection pool + migrations. Every table lives in one schema (default "spa")."""

    def __init__(self, dsn: str, schema: str = "spa", tz: str = "Asia/Kolkata", pool_max: int = 10):
        if not _IDENT.match(schema):
            raise ValueError("schema must be lowercase letters, digits and underscores")
        if not _TZ.match(tz):
            raise ValueError("invalid time zone name")
        self.schema = schema
        migrate(dsn, schema)
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=pool_max, open=True,
            # free/serverless Postgres hosts (Neon...) suspend idle databases and drop their connections:
            # test a connection before handing it out and retire idle ones, instead of failing a user's request
            check=ConnectionPool.check_connection, max_idle=240,
            kwargs={"row_factory": dict_row, "options": f"-c search_path={schema} -c TimeZone={tz}"})
        self.pool.wait(timeout=15)

    def _with(self, fn, sql, args):
        with self.pool.connection() as conn:  # commits on success, rolls back on error
            return fn(Tx(conn), sql, args)

    def all(self, sql, args=()):
        return self._with(lambda t, s, a: t.all(s, a), sql, args)

    def one(self, sql, args=()):
        return self._with(lambda t, s, a: t.one(s, a), sql, args)

    def run(self, sql, args=()):
        return self._with(lambda t, s, a: t.run(s, a), sql, args)

    def insert(self, sql, args=()):
        return self._with(lambda t, s, a: t.insert(s, a), sql, args)

    @contextmanager
    def tx(self):
        """Several statements, one transaction: all commit together or none do."""
        with self.pool.connection() as conn:
            with conn.transaction():
                yield Tx(conn)

    def close(self):
        self.pool.close()


def default_dsn() -> str:
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN is not set, e.g. postgresql://spa:secret@localhost:5432/spa")
    return dsn
