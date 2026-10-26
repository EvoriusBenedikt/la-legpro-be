"""B3: per-workspace private internal references.

Covers the design's tests 1–7: the list endpoint workspace scoping and its
no-workspace == legacy-only default; the upload endpoint writing report_id and
rejecting a malformed one; the compliance and chat retrieval private-arm
narrowing; the no-report_id backward-compat invariant; and that the B2
exclusion still works per decision (b) with no workspace column.

The harness applies migrations upto=13, so chunks.report_id (016) is added
idempotently in an autouse fixture. The embedder/reranker/LLM are monkeypatched
so no ML model loads; chunks are seeded directly (no FE upload path produces
internal chunks). rag_source_exclusions (015) is created/cleaned per test."""
import uuid

import pytest
from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db

DIM = 384
ZERO_VEC = "[" + ",".join(["0"] * DIM) + "]"


def _vec(axis: int) -> str:
    v = ["0"] * DIM
    v[axis] = "1"
    return "[" + ",".join(v) + "]"


@pytest.fixture(autouse=True)
def _chunks_report_id_column():
    # Harness applies migrations upto=13; 016 adds report_id. Add it idempotently
    # so the test DB has the column regardless of the harness bound.
    pg_service.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS report_id TEXT")


@pytest.fixture(autouse=True)
def _b2_table():
    pg_service.execute(
        "CREATE TABLE IF NOT EXISTS rag_source_exclusions ("
        "user_id TEXT NOT NULL, source_ref TEXT NOT NULL, "
        "created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (user_id, source_ref))")
    pg_service.execute("TRUNCATE rag_source_exclusions")
    yield
    pg_service.execute("TRUNCATE rag_source_exclusions")


@pytest.fixture(autouse=True)
def _no_ml(monkeypatch):
    """Pin embed_query to axis-0 so axis-0 chunks win distance; stub reranker."""
    from services import embed_service, rag_service

    monkeypatch.setattr(embed_service, "embed_query", lambda text: _vec(0))
    monkeypatch.setattr(rag_service, "embed_query", lambda text: _vec(0))

    class _Rr:
        def predict(self, pairs):
            return [1.0 - i * 0.01 for i in range(len(pairs))]

    monkeypatch.setattr(rag_service, "get_reranker", lambda: _Rr())


def _no_llm(monkeypatch):
    from services import rag_service
    from routers import compliance
    monkeypatch.setattr(rag_service, "is_regulation_relevant", lambda *a, **k: True)
    monkeypatch.setattr(compliance, "call_glm",
                        lambda *a, **k: '{"status": "SESUAI", "penjelasan": "ok"}')


@pytest.fixture
def seed():
    """Seed regulations/public chunks and workspace-scoped internal chunks."""
    reg_ids, chunk_ids = [], []

    def _reg(axis=0, category="UMUM", judul="UU Test"):
        rows = pg_service.execute_returning(
            "INSERT INTO regulations (judul, nomor, jenis, sektor, status, klasifikasi) "
            "VALUES (%s, %s, 'UU', 'Umum', 'Berlaku', 'Umum') RETURNING id",
            (judul, "n-" + uuid.uuid4().hex[:6]))
        rid = rows[0]["id"]
        reg_ids.append(rid)
        cid = "c-" + uuid.uuid4().hex
        pg_service.execute(
            "INSERT INTO chunks (id, doc_id, text, jenis, nomor, sektor, judul, "
            "visibility, doc_category, embedding) "
            "VALUES (%s, %s, %s, 'UU', %s, 'Umum', %s, 'public', %s, %s::vector)",
            (cid, str(rid), f"isi {judul}", "n1", judul, category, _vec(axis)))
        chunk_ids.append(cid)
        return rid

    def _internal(user_id, report_id=None, axis=0, upload_id=None, fname="int.pdf", n=2):
        upload_id = upload_id or uuid.uuid4().hex[:8]
        for i in range(n):
            cid = f"internal_{upload_id}_{i}"
            pg_service.execute(
                "INSERT INTO chunks (id, doc_id, text, jenis, nomor, sektor, judul, "
                "visibility, user_id, report_id, embedding) "
                "VALUES (%s, NULL, %s, 'Dokumen Internal', %s, 'Internal', %s, "
                "'private', %s::text, %s, %s::vector)",
                (cid, f"isi internal {i}", fname, fname, str(user_id), report_id, _vec(axis)))
            chunk_ids.append(cid)
        return upload_id

    yield type("S", (), {"reg": staticmethod(_reg), "internal": staticmethod(_internal)})
    if chunk_ids:
        pg_service.execute("DELETE FROM chunks WHERE id = ANY(%s)", (chunk_ids,))
    if reg_ids:
        pg_service.execute("DELETE FROM regulations WHERE id = ANY(%s)", (reg_ids,))


@pytest.fixture
def list_client():
    """App mounting auth + rag_sources (+ repository for the public-reg ACL)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import auth
    from routers import rag_sources, repository

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(repository.router)
    app.include_router(rag_sources.router)
    return TestClient(app, client=("10.0.0.2", 50000))


def _user():
    uid = str(uuid.uuid4())
    pg_service.execute(
        "INSERT INTO users (id, username, password_hash, email, role, is_active) "
        "VALUES (%s, %s, 'x', %s, 'pengguna', TRUE)",
        (uid, "u" + uid[:8], uid[:8] + "@t.test"))
    return {"id": uid, "role": "pengguna"}


def _internal_refs(docs):
    return [d["source_ref"] for d in docs if d["source_ref"].startswith("internal:")]


# ---- test 1: list workspace scoping ----------------------------------------

def test_list_workspace_scoping(make_user, mint_token, list_client, seed):
    alice = make_user("alice")
    w1 = seed.internal(alice, report_id="W1", fname="w1.pdf")
    w2 = seed.internal(alice, report_id="W2", fname="w2.pdf")
    legacy = seed.internal(alice, report_id=None, fname="legacy.pdf")
    h = bearer(mint_token(alice))

    r1 = list_client.get("/api/rag-sources/list?report_id=W1", headers=h)
    assert set(_internal_refs(r1.json()["documents"])) == {f"internal:{w1}", f"internal:{legacy}"}

    r2 = list_client.get("/api/rag-sources/list?report_id=W2", headers=h)
    assert set(_internal_refs(r2.json()["documents"])) == {f"internal:{w2}", f"internal:{legacy}"}


# ---- test 2: list no-workspace == legacy only ------------------------------

def test_list_no_workspace_is_legacy_only(make_user, mint_token, list_client, seed):
    alice = make_user("alice")
    w1 = seed.internal(alice, report_id="W1", fname="w1.pdf")
    legacy = seed.internal(alice, report_id=None, fname="legacy.pdf")
    h = bearer(mint_token(alice))

    r = list_client.get("/api/rag-sources/list", headers=h)
    assert _internal_refs(r.json()["documents"]) == [f"internal:{legacy}"]
    assert f"internal:{w1}" not in _internal_refs(r.json()["documents"])

    # '' => None: an empty string behaves identically to no param.
    r_empty = list_client.get("/api/rag-sources/list?report_id=", headers=h)
    assert _internal_refs(r_empty.json()["documents"]) == [f"internal:{legacy}"]


# ---- test 3: upload writes report_id / rejects malformed -------------------

def test_upload_writes_report_id(seed):
    # Seed-then-assert the column, mirroring how the suite seeds chunks directly
    # rather than driving the real extractor/embedder.
    user = _user()
    up = seed.internal(user["id"], report_id="W1", n=1)
    row = pg_service.query_one(
        "SELECT report_id FROM chunks WHERE id = %s", (f"internal_{up}_0",))
    assert row["report_id"] == "W1"
    up_null = seed.internal(user["id"], report_id=None, n=1)
    row_null = pg_service.query_one(
        "SELECT report_id FROM chunks WHERE id = %s", (f"internal_{up_null}_0",))
    assert row_null["report_id"] is None


def test_upload_rejects_invalid_report_id(make_user, mint_token):
    # Validation runs before any file work, so no extractor/embedder is reached.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import auth
    import internal_docs

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(internal_docs.router, prefix="/api")
    c = TestClient(app, client=("10.0.0.2", 50000))

    alice = make_user("alice")
    h = bearer(mint_token(alice))
    r = c.post("/api/upload-internal",
               files={"file": ("x.pdf", b"%PDF-1.4 dummy", "application/pdf")},
               data={"report_id": "bad id with spaces!"}, headers=h)
    assert r.status_code == 400


# ---- test 4: compliance private narrowing ----------------------------------

def _compliance_candidate_nomors(clause, user, report_id):
    from routers.compliance import process_single_pasal
    out = process_single_pasal(clause, "PKS", "A", "B", "umum", "pokok", 0.45,
                               user["id"], set(), report_id)
    return {r["nomor"] for r in out["supporting_regulations"]}


def test_compliance_private_narrowing(seed, monkeypatch):
    _no_llm(monkeypatch)
    user = _user()
    # One chunk each so all three admitted candidates fit the SELECT's LIMIT 3
    # (the W2 ref is removed by the SQL predicate before the limit, not by it).
    seed.reg(axis=0, category="UMUM", judul="Public PKS")           # nomor 'n1'
    seed.internal(user["id"], report_id="W1", fname="w1-ref.pdf", n=1)
    seed.internal(user["id"], report_id="W2", fname="w2-ref.pdf", n=1)
    seed.internal(user["id"], report_id=None, fname="legacy-ref.pdf", n=1)
    clause = {"pasal": "Pasal 1", "deskripsi": "klausul pembayaran yang cukup panjang untuk lolos filter."}

    nomors = _compliance_candidate_nomors(clause, user, "W1")
    # W1 internal + legacy internal + public are candidates; W2 is not.
    assert "w1-ref.pdf" in nomors
    assert "legacy-ref.pdf" in nomors
    assert "w2-ref.pdf" not in nomors
    assert "n1" in nomors  # public reg (seeded nomor='n1')


# ---- test 5: chat private narrowing ----------------------------------------

def test_chat_private_narrowing(seed):
    from services.rag_service import retrieve_contexts
    user = _user()
    rid = seed.reg(axis=0, judul="Public UU")
    w1 = seed.internal(user["id"], report_id="W1", fname="w1.pdf")
    w2 = seed.internal(user["id"], report_id="W2", fname="w2.pdf")
    legacy = seed.internal(user["id"], report_id=None, fname="legacy.pdf")

    got = retrieve_contexts("q", user, hybrid_cag=False, report_id="W1")
    ids = {c["id"] for c in got}
    assert any(i.startswith(f"internal_{w1}_") for i in ids)
    assert not any(i.startswith(f"internal_{w2}_") for i in ids)
    assert any(i.startswith(f"internal_{legacy}_") for i in ids)
    assert str(rid) in {c["doc_id"] for c in got}


# ---- test 6: no-report_id invariant ----------------------------------------

def test_no_report_id_invariant_chat(seed):
    from services.rag_service import retrieve_contexts
    user = _user()
    rid = seed.reg(axis=0, judul="Public UU")
    w1 = seed.internal(user["id"], report_id="W1", fname="w1.pdf")
    legacy = seed.internal(user["id"], report_id=None, fname="legacy.pdf")

    got = retrieve_contexts("q", user, hybrid_cag=False, report_id=None)
    ids = {c["id"] for c in got}
    # Pre-B3 candidate set: public + the user's NULL-report_id private rows only.
    assert str(rid) in {c["doc_id"] for c in got}
    assert any(i.startswith(f"internal_{legacy}_") for i in ids)
    assert not any(i.startswith(f"internal_{w1}_") for i in ids)


def test_no_report_id_invariant_compliance(seed, monkeypatch):
    _no_llm(monkeypatch)
    user = _user()
    seed.reg(axis=0, category="UMUM", judul="Public PKS")
    seed.internal(user["id"], report_id="W1", fname="w1-ref.pdf", n=1)
    seed.internal(user["id"], report_id=None, fname="legacy-ref.pdf", n=1)
    clause = {"pasal": "Pasal 1", "deskripsi": "klausul kerahasiaan yang cukup panjang untuk lolos filter."}

    nomors = _compliance_candidate_nomors(clause, user, None)
    assert "legacy-ref.pdf" in nomors
    assert "w1-ref.pdf" not in nomors
    assert "n1" in nomors


# ---- test 7: exclusion still works per decision (b) ------------------------

def test_exclusion_needs_no_workspace_column(seed):
    from services.rag_service import retrieve_contexts
    user = _user()
    keep = seed.internal(user["id"], report_id="W1", fname="keep.pdf")
    drop = seed.internal(user["id"], report_id="W1", fname="drop.pdf")

    base = retrieve_contexts("q", user, hybrid_cag=False, report_id="W1")
    base_ids = {c["id"] for c in base}
    assert any(i.startswith(f"internal_{drop}_") for i in base_ids)

    pg_service.execute(
        "INSERT INTO rag_source_exclusions (user_id, source_ref) VALUES (%s, %s)",
        (user["id"], f"internal:{drop}"))
    after = retrieve_contexts("q", user, hybrid_cag=False, report_id="W1")
    after_ids = {c["id"] for c in after}
    assert not any(i.startswith(f"internal_{drop}_") for i in after_ids)
    assert any(i.startswith(f"internal_{keep}_") for i in after_ids)
