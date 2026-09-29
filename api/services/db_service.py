"""Metadata database bootstrap & audit logging.

Moved from api/main.py during the Phase 2 refactor. main.py still calls
init_main_db() at import time, in the same position as before (after router
imports, before router mounts).

Migration M3 (cutover): the SQLite DDL this module used to own now lives in
migrations/001-004 -- applied by the postgres container's
docker-entrypoint-initdb.d on fresh volumes, and by the M2/M5 migration
tooling on existing ones. init_main_db() is reduced to a startup
connectivity check plus the idempotent default-taxonomy seed, and audit
logging goes through services.pg_service.
"""
import os

from services import pg_service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# FR-29: default document taxonomy (seeded only when the table is empty)
DEFAULT_TAXONOMY = [
    "Peraturan Pemerintah",
    "Undang-Undang",
    "Peraturan OJK",
    "Surat Edaran OJK",
    "Dokumen Internal",
    "Peraturan Menteri",
    "Regulasi Custom",
]


# ── Init DB for metadata ────────────────────────────────────────────────────
def init_main_db():
    """Verify PostgreSQL is reachable and seed the default taxonomy if empty.

    Raises RuntimeError when the database cannot be reached: the compose
    stack starts the backend only after postgres reports healthy, so a
    failure here means the deployment is broken and must not limp on.
    """
    try:
        row = pg_service.query_one("SELECT version() AS v")
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach PostgreSQL at startup ({e}). "
            "Check DATABASE_URL and that the postgres service is healthy."
        ) from e
    print(f"PostgreSQL ready: {row['v'].split(',')[0]}")

    # Seed default taxonomy if empty
    if pg_service.query_one("SELECT 1 FROM document_taxonomy LIMIT 1") is None:
        for name in DEFAULT_TAXONOMY:
            pg_service.execute(
                "INSERT INTO document_taxonomy (name) VALUES (%s) "
                "ON CONFLICT (name) DO NOTHING",
                (name,),
            )


def log_audit(user_id: str, action_type: str, resource_id: str = "", details: str = ""):
    try:
        pg_service.execute(
            "INSERT INTO audit_logs (user_id, action_type, resource_id, details) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, action_type, resource_id, details),
        )
    except Exception as e:
        print(f"Failed to write audit log: {e}")
