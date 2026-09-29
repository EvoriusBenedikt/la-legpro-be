"""PostgreSQL snapshots & database size reporting (Migration M3 -- cutover).

Replaces the legacy SQLite file-copy backups: the scheduler's nightly loop
and the /api/engineer/backup endpoint both used sqlite3's backup API on
legal_metadata.db / users.db. A backup is now a single tar archive in data/
holding one CSV per table (COPY ... TO STDOUT) plus a manifest, produced
inside one REPEATABLE READ transaction so every table shares a consistent
snapshot.

chunks.embedding is excluded: the legacy backups never covered the vector
store (ChromaDB's files lived outside the SQLite backup scope), and the
column would multiply archive size several-fold.

Legacy *_backup_*.db files stay untouched on disk; listing and retention
only consider the new legpro_backup_*.tar archives.
"""
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime

from psycopg import IsolationLevel

from services import pg_service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")

BACKUP_PREFIX = "legpro_backup_"
BACKUP_SUFFIX = ".tar"

# Table groups mirroring the two former SQLite databases; they back the
# metadata_db_mb / users_db_mb figures the FE monitoring has always shown.
METADATA_TABLES = [
    "regulations", "access_grants", "audit_logs",
    "kg_nodes", "kg_edges", "kg_exclusions", "kg_rebuild_history",
    "system_metrics", "llm_metrics", "document_taxonomy", "chunks",
]
USERS_TABLES = [
    "users", "chat_sessions", "chat_messages",
    "compliance_history", "document_templates", "active_sessions",
]


def group_sizes_mb():
    """(metadata_db_mb, users_db_mb): total relation size per legacy group.

    pg_total_relation_size includes each table's indexes and TOAST, the
    closest PG analogue of the old on-disk .db file sizes.
    """
    row = pg_service.query_one(
        "SELECT "
        "COALESCE(SUM(pg_total_relation_size(c.oid)) FILTER (WHERE c.relname = ANY(%s)), 0)::float "
        "/ (1024 * 1024) AS metadata_mb, "
        "COALESCE(SUM(pg_total_relation_size(c.oid)) FILTER (WHERE c.relname = ANY(%s)), 0)::float "
        "/ (1024 * 1024) AS users_mb "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')",
        (METADATA_TABLES, USERS_TABLES),
    )
    if row is None:
        return 0.0, 0.0
    return round(row["metadata_mb"], 2), round(row["users_mb"], 2)


def _all_tables(conn):
    cur = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema IN ('public', 'scraper') AND table_type = 'BASE TABLE' "
        "ORDER BY table_schema, table_name")
    return [(r["table_schema"], r["table_name"]) for r in cur.fetchall()]


def _columns(conn, schema, table):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table))
    cols = [r["column_name"] for r in cur.fetchall()]
    if schema == "public" and table == "chunks":
        cols = [c for c in cols if c != "embedding"]
    return cols


def create_backup(timestamp: str = None) -> str:
    """Snapshot every table to data/legpro_backup_<ts>.tar; returns the filename."""
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{BACKUP_PREFIX}{ts}{BACKUP_SUFFIX}"
    path = os.path.join(DATA_DIR, name)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix=f".{name}.", dir=DATA_DIR)
    try:
        tables = []
        manifest_tables = []
        with pg_service.get_conn() as conn:
            # One consistent snapshot for every table in the archive.
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            tables = _all_tables(conn)
            for schema, table in tables:
                cols = _columns(conn, schema, table)
                col_sql = ", ".join(f'"{c}"' for c in cols)
                csv_path = os.path.join(tmpdir, f"{schema}.{table}.csv")
                with open(csv_path, "wb") as fh:
                    with conn.cursor().copy(
                        f'COPY (SELECT {col_sql} FROM "{schema}"."{table}") '
                        f"TO STDOUT (FORMAT csv, HEADER true)"
                    ) as copy:
                        for chunk in copy:
                            fh.write(chunk)
                manifest_tables.append({"table": f"{schema}.{table}", "columns": cols})

        manifest = {
            "created_at": datetime.now().isoformat(),
            "database": "legpro",
            "format": "tar of per-table CSVs (COPY TO STDOUT, header row included)",
            "note": "chunks.embedding excluded (legacy backups never covered the "
                    "vector store). Restore = TRUNCATE + COPY FROM per table, "
                    "then reset identity sequences.",
            "tables": manifest_tables,
        }
        manifest_path = os.path.join(tmpdir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

        with tarfile.open(path, "w") as tar:
            for schema, table in tables:
                tar.add(os.path.join(tmpdir, f"{schema}.{table}.csv"),
                        arcname=f"{schema}.{table}.csv")
            tar.add(manifest_path, arcname="manifest.json")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return name


def list_backups():
    """Backup archives newest-first, same keys the FE has always received."""
    backups = []
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.startswith(BACKUP_PREFIX) and f.endswith(BACKUP_SUFFIX):
                path = os.path.join(DATA_DIR, f)
                backups.append({
                    "filename": f,
                    "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
                    "created_at": datetime.fromtimestamp(os.path.getctime(path)).isoformat()
                })
    return sorted(backups, key=lambda x: x["created_at"], reverse=True)


def has_backup_from(day: str) -> bool:
    """True when a legpro_backup_<day>_*.tar archive already exists."""
    if not os.path.exists(DATA_DIR):
        return False
    return any(
        f.startswith(BACKUP_PREFIX) and day in f and f.endswith(BACKUP_SUFFIX)
        for f in os.listdir(DATA_DIR)
    )


def prune_backups(retention: int):
    """Keep the newest `retention` archives (one file per run now, the
    legacy runs produced two sqlite files per run)."""
    archives = sorted(list_backups(), key=lambda x: x["filename"], reverse=True)
    for old in archives[max(1, retention):]:
        try:
            os.remove(os.path.join(DATA_DIR, old["filename"]))
        except OSError:
            pass
