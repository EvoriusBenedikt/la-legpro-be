"""B2: retrieval exclusion in both paths + the source_ref mapper.

Asserts the headline invariant (empty set => current behavior), public-reg and
internal-doc exclusion from the chat (retrieve_contexts) and compliance
(process_single_pasal) paths, and the CAG whole-document branch. The embedder
and reranker are monkeypatched so no ML model loads; chunks are seeded directly
(no FE upload path produces internal chunks). The harness does not cover
rag_source_exclusions, so it is created idempotently and cleaned per test."""
import uuid

import pytest
from services import pg_service

pytestmark = pytest.mark.db

DIM = 384


def _vec(axis: int) -> str:
    """Unit vector with 1.0 at `axis`, else 0 — gives controllable cosine dist."""
    v = ["0"] * DIM
    v[axis] = "1"
    return "[" + ",".join(v) + "]"


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
    """Pin embed_query to axis-0 (so axis-0 chunks win distance) and stub the
    reranker to preserve candidate order."""
    from services import embed_service, rag_service

    monkeypatch.setattr(embed_service, "embed_query", lambda text: _vec(0))
    # rag_service bound embed_query at import (from ... import embed_query), so
    # patch that name too; compliance imports it locally at call time.
    monkeypatch.setattr(rag_service, "embed_query", lambda text: _vec(0))

    class _Rr:
        def predict(self, pairs):
            return [1.0 - i * 0.01 for i in range(len(pairs))]

    monkeypatch.setattr(rag_service, "get_reranker", lambda: _Rr())


@pytest.fixture
def seed():
    """Seed chunks; returns a helper. Cleans up regulations + chunks."""
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

    def _internal(user_id, axis=0, upload_id=None, fname="int.pdf", n=1):
        upload_id = upload_id or uuid.uuid4().hex[:8]
        for i in range(n):
            cid = f"internal_{upload_id}_{i}"
            pg_service.execute(
                "INSERT INTO chunks (id, doc_id, text, jenis, nomor, sektor, judul, "
                "visibility, user_id, embedding) "
                "VALUES (%s, NULL, %s, 'Dokumen Internal', %s, 'Internal', %s, "
                "'private', %s::text, %s::vector)",
                (cid, f"isi internal {i}", fname, fname, str(user_id), _vec(axis)))
            chunk_ids.append(cid)
        return upload_id

    yield type("S", (), {"reg": staticmethod(_reg), "internal": staticmethod(_internal)})
    if chunk_ids:
        pg_service.execute("DELETE FROM chunks WHERE id = ANY(%s)", (chunk_ids,))
    if reg_ids:
        pg_service.execute("DELETE FROM regulations WHERE id = ANY(%s)", (reg_ids,))


def _user():
    uid = str(uuid.uuid4())
    pg_service.execute(
        "INSERT INTO users (id, username, password_hash, email, role, is_active) "
        "VALUES (%s, %s, 'x', %s, 'pengguna', TRUE)",
        (uid, "u" + uid[:8], uid[:8] + "@t.test"))
    return {"id": uid, "role": "pengguna"}


def _no_llm(monkeypatch):
    from services import rag_service
    from routers import compliance
    monkeypatch.setattr(rag_service, "is_regulation_relevant", lambda *a, **k: True)
    # Keep the final verification call off the network; a cited reg is enough.
    monkeypatch.setattr(compliance, "call_glm",
                        lambda *a, **k: '{"status": "SESUAI", "penjelasan": "ok"}')


# ---- mapper ----------------------------------------------------------------

def test_chunk_source_ref():
    from services.rag_service import chunk_source_ref
    assert chunk_source_ref("12", "anything") == "reg:12"
    assert chunk_source_ref(None, "internal_ab12cd34_0") == "internal:ab12cd34"
    assert chunk_source_ref(None, "legacy_0") is None
    assert chunk_source_ref(None, None) is None


# ---- chat path (retrieve_contexts) -----------------------------------------

def test_chat_empty_set_invariant_and_public_exclusion(seed, monkeypatch):
    from services.rag_service import retrieve_contexts
    user = _user()
    rid = seed.reg(axis=0, judul="Target UU")
    seed.reg(axis=1, judul="Other UU")

    # Empty set: Target UU present (hybrid_cag=False so it goes through dense+rerank).
    base = retrieve_contexts("q", user, hybrid_cag=False)
    base_docs = {c["doc_id"] for c in base}
    assert str(rid) in base_docs

    # Exclude the target: it must disappear, others stay.
    pg_service.execute(
        "INSERT INTO rag_source_exclusions (user_id, source_ref) VALUES (%s, %s)",
        (user["id"], f"reg:{rid}"))
    after = retrieve_contexts("q", user, hybrid_cag=False)
    assert str(rid) not in {c["doc_id"] for c in after}


def test_chat_cag_branch_excludes_discovery_winner(seed):
    from services.rag_service import retrieve_contexts
    user = _user()
    rid = seed.reg(axis=0, judul="Small Doc")  # short text => fits the 25k CAG budget

    # CAG path returns the whole document when it wins discovery.
    got = retrieve_contexts("q", user, hybrid_cag=True)
    assert str(rid) in {c["doc_id"] for c in got}

    pg_service.execute(
        "INSERT INTO rag_source_exclusions (user_id, source_ref) VALUES (%s, %s)",
        (user["id"], f"reg:{rid}"))
    after = retrieve_contexts("q", user, hybrid_cag=True)
    assert str(rid) not in {c["doc_id"] for c in after}


def test_chat_internal_exclusion(seed):
    from services.rag_service import retrieve_contexts
    user = _user()
    up = seed.internal(user["id"], axis=0, fname="keep.pdf")
    up2 = seed.internal(user["id"], axis=0, fname="drop.pdf")

    base = retrieve_contexts("q", user, hybrid_cag=False)
    base_ids = {c["id"] for c in base}
    assert any(cid.startswith(f"internal_{up2}_") for cid in base_ids)

    pg_service.execute(
        "INSERT INTO rag_source_exclusions (user_id, source_ref) VALUES (%s, %s)",
        (user["id"], f"internal:{up2}"))
    after = retrieve_contexts("q", user, hybrid_cag=False)
    after_ids = {c["id"] for c in after}
    assert not any(cid.startswith(f"internal_{up2}_") for cid in after_ids)
    # The non-excluded internal upload still returns.
    assert any(cid.startswith(f"internal_{up}_") for cid in after_ids)


# ---- compliance path (process_single_pasal) --------------------------------

def test_compliance_empty_set_and_public_exclusion(seed, monkeypatch):
    _no_llm(monkeypatch)
    from routers.compliance import process_single_pasal
    user = _user()
    rid = seed.reg(axis=0, category="UMUM", judul="Target PKS")

    clause = {"pasal": "Pasal 1", "deskripsi": "klausul pembayaran yang cukup panjang untuk lolos filter."}
    base = process_single_pasal(clause, "PKS", "A", "B", "umum", "pokok", 0.45,
                                user["id"], set())
    base_regs = {r["nomor"] for r in base["supporting_regulations"]}
    assert base_regs  # the public reg was cited

    out = process_single_pasal(clause, "PKS", "A", "B", "umum", "pokok", 0.45,
                               user["id"], {f"reg:{rid}"})
    assert out["supporting_regulations"] == [] and out["status"] == "TIDAK DIATUR"


def test_compliance_internal_exclusion(seed, monkeypatch):
    _no_llm(monkeypatch)
    from routers.compliance import process_single_pasal
    user = _user()
    up = seed.internal(user["id"], axis=0, fname="internal-pks.pdf")

    clause = {"pasal": "Pasal 1", "deskripsi": "klausul kerahasiaan yang cukup panjang untuk lolos filter."}
    # Admitted (internal doc appears as an authoritative reference).
    base = process_single_pasal(clause, "PKS", "A", "B", "umum", "pokok", 0.45,
                                user["id"], set())
    assert base["supporting_regulations"]

    out = process_single_pasal(clause, "PKS", "A", "B", "umum", "pokok", 0.45,
                               user["id"], {f"internal:{up}"})
    assert out["supporting_regulations"] == []
