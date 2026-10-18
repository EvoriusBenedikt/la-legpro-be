"""Process-level app configuration helpers (auth hardening, 2026-10-14).

ENABLE_DOCS (default off): Swagger UI, ReDoc and the OpenAPI schema hand an
attacker a full map of every endpoint, so they are disabled unless the
operator opts in (local dev override: docker-compose.dev.yml sets it true).
"""
import os


def docs_kwargs() -> dict:
    """Keyword arguments for ``FastAPI(...)`` controlling the docs endpoints."""
    if os.getenv("ENABLE_DOCS", "").strip().lower() == "true":
        return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}
