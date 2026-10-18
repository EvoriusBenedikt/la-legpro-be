"""Shared test harness (auth hardening, 2026-10-14).

Run (throwaway Postgres, never the real database):
    docker run -d --name legpro-test-pg -e POSTGRES_PASSWORD=test \
        -e POSTGRES_USER=legpro -e POSTGRES_DB=legpro_test \
        -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
    set TEST_DATABASE_URL=postgresql://legpro:test@127.0.0.1:55432/legpro_test
    .venv\\Scripts\\python -m pytest -q

Tests marked ``db`` are skipped when TEST_DATABASE_URL is unset. The JWT
secret below is an inline dummy, never a real key.
"""
import os
import pathlib
import sys

import pytest

# Must be set BEFORE importing auth / pg_service: auth raises without
# JWT_SECRET, and pg_service reads DATABASE_URL at import time.
os.environ["JWT_SECRET"] = "test-secret-not-real"
TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"
sys.path.insert(0, str(ROOT / "api"))

# bcrypt cost 4 instead of 12 so the suite (hundreds of hashes) stays fast.
# Real hashes verify at whatever cost is embedded in them, so behaviour under
# test is unchanged; only wall time differs. Patched before auth is imported
# so auth._DUMMY_HASH is cheap as well.
import bcrypt  # noqa: E402

_orig_gensalt = bcrypt.gensalt
bcrypt.gensalt = lambda rounds=4, prefix=b"2b": _orig_gensalt(rounds=rounds, prefix=prefix)


def apply_migrations(dsn: str, upto: int = 11) -> None:
    """Apply migrations/001..<upto> in order to ``dsn`` (multi-statement)."""
    import psycopg

    files = sorted(MIGRATIONS.glob("*.sql"))
    with psycopg.connect(dsn, autocommit=True) as conn:
        for f in files:
            if int(f.name.split("_", 1)[0]) <= upto:
                conn.execute(f.read_text(encoding="utf-8"))


def _schema_present() -> bool:
    import psycopg

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.users') IS NOT NULL AND "
            "to_regclass('public.login_attempts') IS NOT NULL"
        ).fetchone()
        return bool(row[0])


if TEST_DB and not _schema_present():
    # auth.py runs init_db() at import and needs document_templates, so the
    # schema has to exist before any test module imports auth.
    apply_migrations(TEST_DB)


def pytest_collection_modifyitems(config, items):
    if TEST_DB:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _clean_db(request):
    """Per-test reset for db-marked tests: empty the auth tables and the
    in-process housekeeping timer."""
    if request.node.get_closest_marker("db") is None or not TEST_DB:
        yield
        return
    from services import pg_service

    pg_service.execute(
        "TRUNCATE users, login_attempts, audit_logs, active_sessions RESTART IDENTITY CASCADE"
    )
    import auth

    auth._last_housekeeping = 0.0
    yield


# ---- helpers -------------------------------------------------------------

@pytest.fixture
def make_user():
    """Insert a user directly. Returns the id."""
    import uuid

    import auth
    from services import pg_service

    def _make(username, password="Str0ng!Passw0rd", role="pengguna", email=None,
              is_active=True):
        uid = str(uuid.uuid4())
        pg_service.execute(
            "INSERT INTO users (id, username, password_hash, email, role, is_active) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, username, auth.get_password_hash(password),
             email or f"{username.lower()}@example.test", role, is_active),
        )
        return uid

    return _make


@pytest.fixture
def client():
    """TestClient over a small app: auth + admin routers. The peer address is
    the testclient's (private/loopback-free ``testclient``), so tests set the
    source IP through the X-Real-IP header only where they patch the peer."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import auth
    from routers import admin

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(admin.router)
    # client=("10.0.0.2", 50000): a private peer, like the nginx container, so
    # X-Real-IP is honoured exactly as in production.
    return TestClient(app, client=("10.0.0.2", 50000))


@pytest.fixture
def login(client):
    def _login(username, password, ip=None):
        headers = {"X-Real-IP": ip} if ip else {}
        return client.post("/api/auth/login",
                           json={"username": username, "password": password},
                           headers=headers)

    return _login


@pytest.fixture
def mint_token():
    """Mint a JWT for a user id, reading the CURRENT token_version unless a
    ``tv`` is given (use tv=None to omit the claim entirely)."""
    import auth
    from services import pg_service

    _unset = object()

    def _mint(user_id, tv=_unset):
        row = pg_service.query_one(
            "SELECT username, email, role, token_version FROM users WHERE id = %s", (user_id,))
        data = {"sub": user_id, "username": row["username"], "email": row["email"],
                "role": row["role"]}
        if tv is _unset:
            data["tv"] = row["token_version"]
        elif tv is not None:
            data["tv"] = tv
        return auth.create_access_token(data)

    return _mint


def bearer(token):
    return {"Authorization": f"Bearer {token}"}
