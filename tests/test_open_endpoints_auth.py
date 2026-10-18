"""T8: the five formerly open endpoints now require a token; role rules (D2);
PDF repository ACL (D6). The LLM is monkeypatched, nothing leaves the box."""
import base64
import uuid

import pytest
from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db

PAYLOAD = {"reg_id": "999999", "nomor": "N-1", "judul": "J"}
PDF_BYTES = b"%PDF-1.4 fake test pdf"
# HTTPBearer answers a missing header with 401 or 403 depending on the
# installed FastAPI version; accept both (the point is: not 200).
NO_TOKEN = (401, 403)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    from routers import chat, compliance, knowledge_graph

    def fake(*a, **k):
        return '["n1"]'

    for mod in (chat, compliance, knowledge_graph):
        monkeypatch.setattr(mod, "call_glm", fake)


@pytest.fixture
def pdfs(tmp_path, monkeypatch):
    """Point serve_pdf at a temp dir and clean the rows we insert."""
    from routers import repository

    monkeypatch.setattr(repository, "PDFS_DIR", str(tmp_path))
    ids = []

    def add(filename, klasifikasi="Umum", status="Berlaku", local_path=None, write=True):
        if write:
            (tmp_path / filename).write_bytes(PDF_BYTES)
        rows = pg_service.execute_returning(
            "INSERT INTO regulations (judul, nomor, status, klasifikasi, local_path) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            ("t", "n-" + uuid.uuid4().hex[:6], status, klasifikasi, local_path or str(tmp_path / filename)))
        rid = rows[0]["id"]
        ids.append(rid)
        return rid

    yield add
    pg_service.execute("DELETE FROM access_grants WHERE doc_id = ANY(%s)", ([str(i) for i in ids],))
    pg_service.execute("DELETE FROM regulations WHERE id = ANY(%s)", (ids,))


def tok(make_user, mint_token, name="alice", role="pengguna"):
    return bearer(mint_token(make_user(name, role=role)))


# ---- missing token ---------------------------------------------------------

def test_missing_token_rejected_everywhere(client, pdfs):
    pdfs("x.pdf")
    calls = [
        client.post("/api/analyze", json=PAYLOAD),
        client.post("/api/analyze-pasals", json=PAYLOAD),
        client.post("/api/check-compliance", files={"file": ("a.xyz", b"x")}),
        client.post("/api/knowledge-graph/analyze-scenario", json={"scenario": "s"}),
        client.get("/api/pdf/x.pdf"),
        client.post("/api/chat", json={"messages": []}),
        client.get("/api/notifications/someone"),
    ]
    assert [r.status_code in NO_TOKEN for r in calls] == [True] * len(calls), \
        [r.status_code for r in calls]


def test_garbage_token_is_401(client):
    h = bearer("not-a-jwt")
    assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 401
    assert client.get("/api/pdf/x.pdf", headers=h).status_code == 401


# ---- with a token ----------------------------------------------------------

def test_pengguna_reaches_every_endpoint(make_user, mint_token, client, pdfs):
    h = tok(make_user, mint_token)
    pdfs("ok.pdf")
    assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 200
    assert client.post("/api/analyze-pasals", json=PAYLOAD, headers=h).status_code == 200
    r = client.post("/api/knowledge-graph/analyze-scenario", json={"scenario": "s"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] in ("success", "error")
    # unsupported extension -> the handler's own 400 proves auth+role+limiter passed
    # without writing a temp file or running the LLM pipeline
    r = client.post("/api/check-compliance", files={"file": ("a.xyz", b"x")}, headers=h)
    assert r.status_code == 400 and "Format file tidak didukung" in r.json()["detail"]
    r = client.get("/api/pdf/ok.pdf", headers=h)
    assert r.status_code == 200
    assert set(r.json()) == {"filename", "data"}  # JSON shape unchanged
    assert r.json()["filename"] == "ok.pdf"
    assert base64.b64decode(r.json()["data"]) == PDF_BYTES


def test_insinyur_ti_allowed_on_drawer_endpoints_blocked_on_business_ones(
        make_user, mint_token, client, pdfs):
    h = tok(make_user, mint_token, "eng", "insinyur ti")
    pdfs("ok.pdf")
    assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 200
    assert client.post("/api/analyze-pasals", json=PAYLOAD, headers=h).status_code == 200
    assert client.get("/api/pdf/ok.pdf", headers=h).status_code == 200
    assert client.post("/api/check-compliance", files={"file": ("a.xyz", b"x")},
                       headers=h).status_code == 403
    assert client.post("/api/knowledge-graph/analyze-scenario", json={"scenario": "s"},
                       headers=h).status_code == 403


def test_deactivated_account_token_rejected(make_user, mint_token, client):
    uid = make_user("gone")
    token = mint_token(uid)
    pg_service.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (uid,))
    assert client.post("/api/analyze", json=PAYLOAD, headers=bearer(token)).status_code == 401


# ---- PDF ACL (D6) ----------------------------------------------------------

def test_pdf_admin_is_403(make_user, mint_token, client, pdfs):
    pdfs("pub.pdf", "Umum")
    h = tok(make_user, mint_token, "root", "admin")
    assert client.get("/api/pdf/pub.pdf", headers=h).status_code == 403


def test_pdf_unknown_file_is_404_even_if_it_exists_on_disk(make_user, mint_token, client, pdfs, tmp_path):
    (tmp_path / "orphan.pdf").write_bytes(PDF_BYTES)  # on disk, no regulations row
    h = tok(make_user, mint_token)
    assert client.get("/api/pdf/orphan.pdf", headers=h).status_code == 404
    assert client.get("/api/pdf/nothing-here.pdf", headers=h).status_code == 404


@pytest.mark.parametrize("klasifikasi,role,expected", [
    ("Umum", "pengguna", 200),
    ("Rahasia", "pengguna", 403),
    ("Rahasia", "manajer", 200),
    ("Terbatas", "pengguna", 403),
    ("Terbatas", "manajer", 403),
    ("Terbatas", "direktur", 200),
    ("Publik", "pengguna", 403),   # not in the repository's allowed list either
    (None, "pengguna", 403),       # NULL matches nothing, like GET /api/repository
])
def test_pdf_classification_matrix(make_user, mint_token, client, pdfs, klasifikasi, role, expected):
    pdfs("doc.pdf", klasifikasi)
    h = tok(make_user, mint_token, "u", role)
    assert client.get("/api/pdf/doc.pdf", headers=h).status_code == expected


def test_pdf_grant_opens_a_restricted_doc_until_it_expires(make_user, mint_token, client, pdfs):
    rid = pdfs("sec.pdf", "Terbatas")
    uid = make_user("alice")
    h = bearer(mint_token(uid))
    assert client.get("/api/pdf/sec.pdf", headers=h).status_code == 403
    gid = str(uuid.uuid4())
    pg_service.execute(
        "INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason, expires_at) "
        "VALUES (%s, %s, %s, %s, 'test', NOW() + interval '1 day')", (gid, str(rid), uid, uid))
    assert client.get("/api/pdf/sec.pdf", headers=h).status_code == 200
    pg_service.execute("UPDATE access_grants SET expires_at = NOW() - interval '1 minute' WHERE id = %s", (gid,))
    assert client.get("/api/pdf/sec.pdf", headers=h).status_code == 403


def test_pdf_grant_is_per_user(make_user, mint_token, client, pdfs):
    rid = pdfs("sec.pdf", "Terbatas")
    a, b = make_user("alice"), make_user("bob")
    pg_service.execute(
        "INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason) "
        "VALUES (%s, %s, %s, %s, 'test')", (str(uuid.uuid4()), str(rid), a, a))
    assert client.get("/api/pdf/sec.pdf", headers=bearer(mint_token(a))).status_code == 200
    assert client.get("/api/pdf/sec.pdf", headers=bearer(mint_token(b))).status_code == 403


@pytest.mark.parametrize("status", ["Menunggu Konfirmasi", "Memproses"])
@pytest.mark.parametrize("role,expected", [
    ("pengguna", 403), ("manajer", 403), ("direktur", 403),
    ("sekretaris perusahaan", 200), ("dewa", 200),
])
def test_pdf_queue_rows_only_for_sekretaris_level(make_user, mint_token, client, pdfs, status, role, expected):
    pdfs("queued.pdf", "Umum", status=status)
    h = tok(make_user, mint_token, "u", role)
    assert client.get("/api/pdf/queued.pdf", headers=h).status_code == expected


def test_pdf_windows_local_path_and_traversal(make_user, mint_token, client, pdfs, tmp_path):
    (tmp_path / "win.pdf").write_bytes(PDF_BYTES)
    pdfs("win.pdf", "Umum", local_path="C:\\data\\pdfs\\kemenkeu\\win.pdf", write=False)
    h = tok(make_user, mint_token)
    assert client.get("/api/pdf/win.pdf", headers=h).status_code == 200
    # traversal attempts are reduced to a basename and then need a matching row
    assert client.get("/api/pdf/..%2F..%2Fetc%2Fpasswd", headers=h).status_code == 404
    assert client.get("/api/pdf/..%5C..%5Cwin.pdf", headers=h).status_code == 200  # basename win.pdf


def test_pdf_row_viewable_but_file_missing_is_404(make_user, mint_token, client, pdfs):
    pdfs("gone.pdf", "Umum", write=False)
    assert client.get("/api/pdf/gone.pdf", headers=tok(make_user, mint_token)).status_code == 404
