"""Bootstrap for host/container operational scripts (Migration M4).

The peripheral tools (scraper/, vector_db/, check_db.py, evaluate_accuracy.py)
were cut over from SQLite+ChromaDB to PostgreSQL+pgvector in M4. They run in
two places:

  * inside the backend container (supported default -- psycopg,
    sentence-transformers and the vendored MiniLM weights are all present)::

        docker exec legpro-backend python /app/vector_db/audit_kb.py

  * on the host (metadata-only tools; needs ``pip install psycopg[binary]``
    into la-legpro-be/.venv). PG is reachable on 127.0.0.1:5432 via the
    loopback-only compose port binding.

Scripts call ``bootstrap()`` once at startup. It:
  1. loads la-legpro-be/.env (bind-mounted at /app/.env in the container);
  2. puts api/ and parser/ on sys.path so ``services.*`` and ``pdf_parser``
     imports resolve from anywhere (mirrors api/main.py);
  3. sets DATABASE_URL when absent -- the container already has it from
     docker-compose.yml; host runs build a loopback URL from POSTGRES_PASSWORD;
  4. registers pg_service.close_pool() at exit so pooled connections do not
     linger after short-lived scripts.
"""
import atexit
import os
import sys
from urllib.parse import quote_plus

# la-legpro-be/ on the host, /app in the container (this file sits in both).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def bootstrap(needs_api: bool = True, needs_parser: bool = False) -> None:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(BASE_DIR, ".env"))

    for name, wanted in (("api", needs_api), ("parser", needs_parser)):
        if wanted:
            path = os.path.join(BASE_DIR, name)
            if path not in sys.path:
                sys.path.insert(0, path)

    if not os.environ.get("DATABASE_URL"):
        password = os.getenv("POSTGRES_PASSWORD", "legpro")
        os.environ["DATABASE_URL"] = (
            f"postgresql://legpro:{quote_plus(password)}@127.0.0.1:5432/legpro"
        )

    if needs_api:
        from services import pg_service

        atexit.register(pg_service.close_pool)


def resolve_data_path(path):
    """Map a regulations.local_path value onto a path that exists HERE.

    The corpus was scraped on a Windows host, so the rows migrated in M2
    carry host-absolute paths (C:\\Users\\...\\la-legpro-be\\data\\pdfs\\x.pdf)
    while the backend container sees the same files under /app/data (bind
    mount). Resolution order:

      1. the path as-is (native runs, and rows written post-M3 which already
         use container paths);
      2. everything after the last ``la-legpro-be/`` segment, re-rooted onto
         BASE_DIR;
      3. everything after the last ``data/pdfs/`` segment, re-rooted onto
         BASE_DIR/data/pdfs.

    Returns the original value unchanged when nothing resolves -- callers
    treat that as "file missing", exactly like the legacy host runs did.
    """
    if not path:
        return path
    if os.path.exists(path):
        return path
    norm = path.replace("\\", "/")
    for marker in ("la-legpro-be/", "data/pdfs/"):
        idx = norm.rfind(marker)
        if idx == -1:
            continue
        if marker == "la-legpro-be/":
            tail = norm[idx + len(marker):]
        else:
            tail = "data/pdfs/" + norm[idx + len(marker):]
        candidate = os.path.join(BASE_DIR, *tail.split("/"))
        if os.path.exists(candidate):
            return candidate
    return path
