"""B2: /api/rag-sources router — list shape, exclusion round-trip, auth
scoping, source_ref round-trip, and validation. The harness's
apply_migrations(upto=13)/_clean_db do not cover rag_source_exclusions, so
the table is created idempotently in a fixture and cleaned per test."""
import uuid

import pytest
from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _b2_table():
    pg_service.execute(
        "CREATE TABLE IF NOT EXISTS rag_source_exclusions ("
        "user_id TEXT NOT NULL, source_ref TEXT NOT NULL, "
        "created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (user_id, source_ref))")
    pg_service.execute("TRUNCATE rag_source_exclusions")
    yield
    pg_service.execute("TRUNCATE rag_source_exclusions")


@pytest.fixture
def client():
    """Local app mounting auth + rag_sources (+ repository, which rag_sources
    reuses for the public-reg ACL)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import auth
    from routers import rag_sources, repository

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(repository.router)
    app.include_router(rag_sources.router)
    return TestClient(app, client=("10.0.0.2", 50000))


def _tok(make_user, mint_token, name, role="pengguna"):
    return bearer(mint_token(make_user(name, role=role)))


ZERO_VEC = "[" + ",".join(["0"] * 384) + "]"


@pytest.fixture
def seed_internal():
    """Seed one internal upload's chunks (doc_id NULL, private, caller-owned)."""
    ids = []

    def _seed(user_id, upload_id=None, n=2, fname="kontrak.pdf"):
        upload_id = upload_id or uuid.uuid4().hex[:8]
        for i in range(n):
            cid = f"internal_{upload_id}_{i}"
            pg_service.execute(
                "INSERT INTO chunks (id, doc_id, text, jenis, nomor, sektor, judul, "
                "visibility, user_id, embedding) "
                "VALUES (%s, NULL, %s, 'Dokumen Internal', %s, 'Internal', %s, "
                "'private', %s::text, %s::vector)",
                (cid, f"chunk {i}", fname, fname, str(user_id), ZERO_VEC))
            ids.append(cid)
        return upload_id

    yield _seed
    if ids:
        pg_service.execute("DELETE FROM chunks WHERE id = ANY(%s)", (ids,))


def test_list_shape_and_internal_scoping(make_user, mint_token, client, seed_internal):
    alice = make_user("alice")
    bob = make_user("bob")
    up = seed_internal(alice, fname="rahasia.pdf")
    seed_internal(bob, fname="bobdoc.pdf")

    r = client.get("/api/rag-sources/list", headers=bearer(mint_token(alice)))
    assert r.status_code == 200
    docs = r.json()["documents"]
    internal = [d for d in docs if d["source_ref"].startswith("internal:")]
    # Only alice's own upload surfaces, with source_ref + active present.
    assert [d["source_ref"] for d in internal] == [f"internal:{up}"]
    assert internal[0]["active"] is True
    assert internal[0]["judul"] == "rahasia.pdf"
    assert all("source_ref" in d and "active" in d for d in docs)


def test_exclusions_round_trip(make_user, mint_token, client):
    h = _tok(make_user, mint_token, "alice")
    assert client.get("/api/rag-sources/exclusions", headers=h).json()["excluded"] == []
    assert client.post("/api/rag-sources/exclusions",
                       json={"source_ref": "reg:12", "excluded": True}, headers=h).status_code == 200
    assert client.get("/api/rag-sources/exclusions", headers=h).json()["excluded"] == ["reg:12"]
    # un-exclude
    assert client.post("/api/rag-sources/exclusions",
                       json={"source_ref": "reg:12", "excluded": False}, headers=h).status_code == 200
    assert client.get("/api/rag-sources/exclusions", headers=h).json()["excluded"] == []


def test_bulk_set(make_user, mint_token, client):
    h = _tok(make_user, mint_token, "alice")
    client.post("/api/rag-sources/exclusions",
                json={"set_excluded": ["reg:1", "reg:2", "reg:3"]}, headers=h)
    assert set(client.get("/api/rag-sources/exclusions", headers=h).json()["excluded"]) == {"reg:1", "reg:2", "reg:3"}
    client.post("/api/rag-sources/exclusions", json={"set_included": ["reg:2"]}, headers=h)
    assert set(client.get("/api/rag-sources/exclusions", headers=h).json()["excluded"]) == {"reg:1", "reg:3"}


def test_auth_scoping(make_user, mint_token, client):
    a = bearer(mint_token(make_user("alice")))
    b = bearer(mint_token(make_user("bob")))
    client.post("/api/rag-sources/exclusions", json={"source_ref": "reg:5", "excluded": True}, headers=a)
    assert client.get("/api/rag-sources/exclusions", headers=a).json()["excluded"] == ["reg:5"]
    # Bob never sees or is affected by Alice's exclusion.
    assert client.get("/api/rag-sources/exclusions", headers=b).json()["excluded"] == []


def test_source_ref_round_trip(make_user, mint_token, client, seed_internal):
    from routers.rag_sources import _valid_source_ref
    alice = make_user("alice")
    up = seed_internal(alice)
    r = client.get("/api/rag-sources/list", headers=bearer(mint_token(alice)))
    internal = next(d for d in r.json()["documents"] if d["source_ref"] == f"internal:{up}")
    # The emitted ref must pass the POST validator (shown => deselectable).
    assert _valid_source_ref(internal["source_ref"]) is True


@pytest.mark.parametrize("body", [
    {"source_ref": "reg:abc", "excluded": True},
    {"source_ref": "internal:xyz", "excluded": True},
    {"source_ref": "../", "excluded": True},
    {"source_ref": "", "excluded": True},
    {"set_excluded": ["reg:0"]},
    {"source_ref": "reg:1"},   # missing excluded
    {},                         # empty-change
])
def test_validation_422(make_user, mint_token, client, body):
    h = _tok(make_user, mint_token, "alice")
    assert client.post("/api/rag-sources/exclusions", json=body, headers=h).status_code == 422


def test_valid_ref_persists(make_user, mint_token, client):
    h = _tok(make_user, mint_token, "alice")
    assert client.post("/api/rag-sources/exclusions",
                       json={"source_ref": "reg:1", "excluded": True}, headers=h).status_code == 200
    assert client.get("/api/rag-sources/exclusions", headers=h).json()["excluded"] == ["reg:1"]
