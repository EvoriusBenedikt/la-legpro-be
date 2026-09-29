"""PostgreSQL connection pool & query helpers (Migration M1 — foundation).

Replaces the per-call ``sqlite3.connect(...)`` pattern used across the API.
Built on psycopg 3 with a thread-safe psycopg_pool.ConnectionPool.

Deliberately synchronous to match the existing sync-in-async FastAPI style
(the sqlite3 code it replaces was equally blocking); routers are cut over to
these helpers incrementally in M3. Until then this module is dormant: the
pool is created lazily on first use, so importing it changes nothing.

Conventions for call sites (M3):
  * ``?`` placeholders become ``%s``.
  * Rows come back as dicts (psycopg ``dict_row``); code that used
    ``sqlite3.Row`` keeps working via ``row["col"]``, code that used
    positional ``row[0]`` must switch to names.
  * ``datetime('now')`` comparisons become ``NOW()``.
  * Literal ``%`` in parameterized SQL (e.g. ``LIKE 'prefix%'``) must be
    doubled to ``%%`` — psycopg parses placeholders whenever params are given.
  * Vector values are passed as their literal text form and cast, e.g.
    ``ORDER BY embedding <=> %s::vector`` with ``"[0.1,0.2,...]"``.

Env:
  DATABASE_URL  full libpq-style URL (default targets the compose service)
  PG_POOL_MIN / PG_POOL_MAX  pool bounds (default 2 / 10)
"""
import os
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    # 127.0.0.1, not localhost: on Windows localhost resolves to ::1 first,
    # and the compose port binding is IPv4-only.
    "postgresql://legpro:legpro@127.0.0.1:5432/legpro",
)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=int(os.getenv("PG_POOL_MIN", "2")),
            max_size=int(os.getenv("PG_POOL_MAX", "10")),
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Close the pool (call from app shutdown once wired in M3)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn():
    """Borrow a pooled connection.

    The pool commits the transaction when the block exits cleanly and rolls
    back on exception — call sites no longer need explicit commit()/close().
    """
    with get_pool().connection() as conn:
        yield conn


def query(sql: str, params=None) -> list[dict]:
    """Run a SELECT and return all rows as dicts."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params=None) -> dict | None:
    """Run a SELECT and return the first row as a dict, or None."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params=None) -> int:
    """Run one INSERT/UPDATE/DELETE; returns affected row count."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_many(sql: str, seq_of_params) -> None:
    """Run the same statement for many parameter tuples in one transaction."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(sql, seq_of_params)


def execute_returning(sql: str, params=None) -> list[dict]:
    """Run INSERT/UPDATE/DELETE ... RETURNING and return the result rows."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def vector_literal(embedding) -> str:
    """Format a sequence of floats as a pgvector literal for ``%s::vector``.

    Avoids a hard dependency on the ``pgvector`` Python package: the text
    form ``[v1,v2,...]`` is exactly what the vector type input parser wants.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
