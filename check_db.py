"""Database introspection tool (Migration M4: was a SQLite schema dump).

Lists every table in the public + scraper schemas of the legpro PostgreSQL
database with row counts and columns -- the same quick orientation dump the
legacy SQLite version printed for legal_metadata.db / ojk_metadata.db.

Run in the container:  docker exec legpro-backend python /app/check_db.py
Run on the host (needs psycopg in .venv):  .venv\\Scripts\\python check_db.py
"""
import script_env

script_env.bootstrap()

from services import pg_service

tables = pg_service.query(
    "SELECT table_schema, table_name FROM information_schema.tables "
    "WHERE table_schema IN ('public', 'scraper') AND table_type = 'BASE TABLE' "
    "ORDER BY table_schema, table_name"
)

for t in tables:
    schema, table = t["table_schema"], t["table_name"]
    # Table names come from information_schema (never user input), so the
    # qualified-name interpolation below is safe -- identifiers cannot be
    # passed as query parameters.
    count = pg_service.query_one(f'SELECT COUNT(*) AS n FROM {schema}."{table}"')["n"]
    cols = pg_service.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    )
    col_names = [c["column_name"] for c in cols]
    print(f'{schema}."{table}": {count} rows, cols={col_names}')
