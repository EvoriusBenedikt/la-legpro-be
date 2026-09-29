"""Shared helpers for the LA LegPro data-migration scripts (M2).

Used by migrate_data.py and verify_migration.py. Deliberately self-contained
(stdlib + psycopg; chromadb is imported lazily by callers) so the whole
migrations/ folder can be copied to instance-3 as-is (M5 runbook).

Safety conventions:
  * SQLite sources are opened read-only (URI mode=ro).
  * Timestamps: legacy stores keep TEXT 'YYYY-MM-DD HH:MM:SS' (UTC); PG columns
    are TIMESTAMPTZ. parse_ts() validates every value and raises loudly on
    anything unparseable — silent NULLs would corrupt history tables.
  * Never print credential/PII values (password_hash, email, ip_address);
    callers redact them in mismatch reports.
"""
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

# Disable chromadb telemetry before any caller imports it.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

DEFAULT_DATABASE_URL = "postgresql://legpro:legpro@127.0.0.1:5432/legpro"

SENSITIVE_COLUMNS = {"password_hash", "email", "ip_address"}


class MigrationError(RuntimeError):
    """Fatal, user-facing migration error (aborts the run)."""


def utf8_console():
    """Windows consoles default to cp1252; our data is Indonesian text."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def database_url(cli_value=None):
    return cli_value or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL


def connect_pg(url):
    """Connect with autocommit=True — deliberate, load-bearing choice.

    With autocommit=False, any statement executed OUTSIDE a
    ``with conn.transaction():`` block (e.g. a bookkeeping ``SELECT count(*)``)
    leaves an implicit transaction open; psycopg3 then treats every subsequent
    transaction() block as NESTED (savepoint, released on exit — no COMMIT),
    and ``close()`` silently rolls the whole run back. That exact bug lost an
    entire dry-run load (caught by verify_migration.py before any production
    use). With autocommit=True every transaction() block is a real
    BEGIN/COMMIT boundary and lone statements are harmless. ``conn.commit()``
    calls elsewhere in these scripts become no-ops, which is safe.
    """
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def connect_sqlite_ro(path):
    if not os.path.exists(path):
        raise MigrationError("source database not found: %s" % path)
    con = sqlite3.connect("file:" + path.replace(os.sep, "/") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def sqlite_has_table(con, name):
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def parse_ts(v):
    """SQLite TEXT/int timestamp (UTC) -> timezone-aware datetime. '' / None -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("unparseable timestamp: %r" % v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_date(v):
    """'YYYY-MM-DD' (or datetime) -> date. '' / None -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        raise ValueError("unparseable date: %r" % v)


def to_bool(v):
    """SQLite 1/0/'true'/... -> bool. '' / None -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "t", "y")


def vector_literal(embedding):
    """Format floats as a pgvector text literal for ``%s::vector``.

    %.9g round-trips float32 exactly (Chroma and pgvector both store float32),
    and avoids a dependency on the ``pgvector`` Python package.
    """
    return "[" + ",".join("%.9g" % float(x) for x in embedding) + "]"


def parse_vector_literal(text):
    """Inverse of vector_literal: '[0.1,0.2]' -> [float]. (pg returns text form.)"""
    import json
    return [float(x) for x in json.loads(text)]


TSV_TEXT_CAP = 30000  # chars of text indexed by content_tsv; sync with 003_chunks_tsv_cap.sql


def ensure_schema_extras(conn):
    """Apply the 002/003 migration SQL equivalents (idempotent).

    Fresh PG volumes get them automatically via /docker-entrypoint-initdb.d;
    already-initialized volumes (like the dev one) get them here. Requires
    the connection's dict_row row factory.
    """
    with conn.cursor() as cur:
        # 002: sparse_legacy flag (legacy FTS5 corpus membership)
        cur.execute(
            "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS "
            "sparse_legacy BOOLEAN NOT NULL DEFAULT FALSE"
        )
        # 003: cap the generated tsvector at the first TSV_TEXT_CAP chars —
        # PG tsvector has a hard 1 MB limit and the live corpus contains
        # multi-MB mega-document chunks that break it.
        cur.execute(
            "SELECT generation_expression AS expr FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'chunks' "
            "AND column_name = 'content_tsv'"
        )
        row = cur.fetchone()
        expr = row["expr"] if row else None
        if expr is None:
            raise MigrationError(
                "chunks.content_tsv not found — apply migrations/001_schema.sql first")
        if str(TSV_TEXT_CAP) not in expr:
            cur.execute("ALTER TABLE chunks DROP COLUMN content_tsv")
            cur.execute(
                "ALTER TABLE chunks ADD COLUMN content_tsv tsvector GENERATED ALWAYS AS "
                "(to_tsvector('simple', left(coalesce(text, ''), %d))) STORED" % TSV_TEXT_CAP
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (content_tsv)"
            )
    conn.commit()


def redact(col, value, limit=60):
    """Display-safe rendering of a column value for reports."""
    if col in SENSITIVE_COLUMNS:
        return "<redacted>"
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value
