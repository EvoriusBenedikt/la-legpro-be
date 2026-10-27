"""Migration 017: empty workspaces ("new workspace" without a document).

Covers the home page's "add a clean workspace" flow end to end on the API
surface: minting an empty workspace, it appearing as a 0-document card,
the first upload joining it under the SAME id (no re-rooting, no duplicate
card), the documents endpoint resolving a workspace id (empty and populated),
foreign-user 404s on every path, and cascade deletes (empty + populated).

The harness applies migrations up to 13, so the notebooks table (017) is
created/truncated idempotently in a fixture, same shape as the B3 suite's
chunks.report_id handling. embed_documents is monkeypatched so no ML model
loads; compliance retrieval is out of scope here (chunk narrowing is covered
by test_b3_report_scoping.py)."""
import uuid

import pytest
from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _notebooks_table():
    # Harness applies migrations up to 13; add 014's grouping column and the
    # 17 tables idempotently, B3-suite style (chunks.report_id handling).
    pg_service.execute("ALTER TABLE compliance_history ADD COLUMN IF NOT EXISTS notebook_id TEXT")
    pg_service.execute("UPDATE compliance_history SET notebook_id = id WHERE notebook_id IS NULL")
    pg_service.execute(
        "CREATE TABLE IF NOT EXISTS notebooks ("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    pg_service.execute(
        "CREATE INDEX IF NOT EXISTS idx_notebooks_user ON notebooks (user_id, created_at)")
    # This file is the only compliance_history user in the suite; start each
    # test from an empty table so card lists never see leftovers.
    pg_service.execute("TRUNCATE notebooks, compliance_history")
    yield
    pg_service.execute("TRUNCATE notebooks, compliance_history")


@pytest.fixture
def hist_client(monkeypatch):
    """TestClient over the history router only (conftest's app doesn't mount
    it). embed_documents is stubbed so saving a report never loads MiniLM."""
    monkeypatch.setattr("history.embed_documents", lambda texts: [[] for _ in texts])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import history

    app = FastAPI()
    app.include_router(history.router, prefix="/api", tags=["history"])
    return TestClient(app, client=("10.0.0.2", 50000))


@pytest.fixture
def save_report():
    """Build a minimal ComplianceReportSave body with a unique filename."""

    def _save(notebook_id=None):
        suffix = uuid.uuid4().hex[:8]
        body = {
            "filename": f"pks_{suffix}.pdf",
            "company_name": f"PT Test {suffix}",
            "expiration_date": None,
            "results": {"summary": {"jenis_dokumen": "PKS"}, "results": []},
        }
        if notebook_id:
            body["notebook_id"] = notebook_id
        return body

    return _save


def _clean_reports(user_id):
    pg_service.execute("DELETE FROM compliance_history WHERE user_id = %s", (user_id,))


def test_create_empty_workspace_appears_as_zero_doc_card(hist_client, mint_token, make_user):
    uid = make_user("ws_maker")
    r = hist_client.post("/api/compliance-history/notebooks",
                         json={"name": "Proyek Alpha"}, headers=bearer(mint_token(uid)))
    assert r.status_code == 200, r.text
    nb = r.json()
    assert uuid.UUID(nb["notebook_id"])  # a fresh uuid, not any document's id
    assert nb["name"] == "Proyek Alpha"

    cards = hist_client.get("/api/compliance-history/notebooks",
                            headers=bearer(mint_token(uid))).json()["notebooks"]
    mine = [c for c in cards if c["id"] == nb["notebook_id"]]
    assert len(mine) == 1
    assert mine[0]["document_count"] == 0
    assert mine[0]["company_name"] == "Proyek Alpha"


def test_create_default_and_blank_names(hist_client, mint_token, make_user):
    uid = make_user("ws_names")
    r1 = hist_client.post("/api/compliance-history/notebooks",
                          json={}, headers=bearer(mint_token(uid)))
    r2 = hist_client.post("/api/compliance-history/notebooks",
                          json={"name": "   "}, headers=bearer(mint_token(uid)))
    assert r1.status_code == 200 and r2.status_code == 200, r1.text
    assert r1.json()["name"] == "Untitled workspace"
    assert r2.json()["name"] == "Untitled workspace"


def test_first_upload_joins_workspace_under_same_id(hist_client, mint_token, make_user, save_report):
    uid = make_user("ws_join")
    headers = bearer(mint_token(uid))
    nb_id = hist_client.post("/api/compliance-history/notebooks",
                             json={"name": "Proyek Beta"}, headers=headers).json()["notebook_id"]

    body = save_report()
    body["notebook_id"] = nb_id
    saved = hist_client.post("/api/compliance-history", json=body, headers=headers)
    assert saved.status_code == 200, saved.text
    doc_id = saved.json()["report_id"]
    assert saved.json()["notebook_id"] == nb_id  # identity stays the workspace id

    # One card for the workspace (not two: the new document is NOT also a
    # re-rooted single-doc notebook because its notebook_id is not its own id).
    cards = hist_client.get("/api/compliance-history/notebooks", headers=headers).json()["notebooks"]
    per_id = [c for c in cards if c["id"] == nb_id]
    others = [c for c in cards if c["id"] == doc_id]
    assert len(per_id) == 1 and not others
    assert per_id[0]["document_count"] == 1

    # The documents endpoint resolves the workspace id and lists the doc.
    docs = hist_client.get(f"/api/compliance-history/{nb_id}/documents", headers=headers).json()
    assert [d["id"] for d in docs["documents"]] == [doc_id]
    assert docs["notebook_name"] == "Proyek Beta"

    _clean_reports(uid)


def test_documents_endpoint_reports_name_for_empty_workspace(hist_client, mint_token, make_user):
    uid = make_user("ws_empty")
    headers = bearer(mint_token(uid))
    nb_id = hist_client.post("/api/compliance-history/notebooks",
                             json={"name": "Kosong"}, headers=headers).json()["notebook_id"]
    docs = hist_client.get(f"/api/compliance-history/{nb_id}/documents", headers=headers)
    assert docs.status_code == 200, docs.text
    assert docs.json()["documents"] == []
    assert docs.json()["notebook_name"] == "Kosong"


def test_foreign_user_workspace_is_404_on_every_path(hist_client, mint_token, make_user, save_report):
    owner = make_user("ws_owner")
    intruder = make_user("ws_intruder")
    owner_h = bearer(mint_token(owner))
    intruder_h = bearer(mint_token(intruder))
    nb_id = hist_client.post("/api/compliance-history/notebooks",
                             json={"name": "Rahasia"}, headers=owner_h).json()["notebook_id"]

    # Upload into someone else's workspace -> 404 (notebook not found).
    body = save_report()
    body["notebook_id"] = nb_id
    assert hist_client.post("/api/compliance-history", json=body, headers=intruder_h).status_code == 404

    assert hist_client.get(f"/api/compliance-history/{nb_id}/documents", headers=intruder_h).status_code == 404
    assert hist_client.delete(f"/api/compliance-history/{nb_id}", headers=intruder_h).status_code == 404


def test_delete_empty_workspace(hist_client, mint_token, make_user):
    uid = make_user("ws_del")
    headers = bearer(mint_token(uid))
    nb_id = hist_client.post("/api/compliance-history/notebooks",
                             json={"name": "Sekali Pakai"}, headers=headers).json()["notebook_id"]
    assert hist_client.delete(f"/api/compliance-history/{nb_id}", headers=headers).status_code == 200
    row = pg_service.query_one("SELECT id FROM notebooks WHERE id = %s", (nb_id,))
    assert row is None
    cards = hist_client.get("/api/compliance-history/notebooks", headers=headers).json()["notebooks"]
    assert all(c["id"] != nb_id for c in cards)


def test_delete_populated_workspace_cascades_documents(hist_client, mint_token, make_user, save_report):
    uid = make_user("ws_cascade")
    headers = bearer(mint_token(uid))
    nb_id = hist_client.post("/api/compliance-history/notebooks",
                             json={"name": "Kaskade"}, headers=headers).json()["notebook_id"]

    body = save_report()
    body["notebook_id"] = nb_id
    doc_id = hist_client.post("/api/compliance-history", json=body, headers=headers).json()["report_id"]

    assert hist_client.delete(f"/api/compliance-history/{nb_id}", headers=headers).status_code == 200
    assert pg_service.query_one("SELECT id FROM notebooks WHERE id = %s", (nb_id,)) is None
    assert pg_service.query_one("SELECT id FROM compliance_history WHERE id = %s", (doc_id,)) is None

    _clean_reports(uid)


def test_legacy_document_rooted_notebook_untouched(hist_client, mint_token, make_user, save_report):
    """Backward compat: an upload without notebook_id still roots its own
    document-id notebook, and deleting its root cascades the legacy way."""
    uid = make_user("ws_legacy")
    headers = bearer(mint_token(uid))
    body = save_report()
    root_id = hist_client.post("/api/compliance-history", json=body, headers=headers).json()["report_id"]

    child = save_report()
    child["notebook_id"] = root_id
    child_id = hist_client.post("/api/compliance-history", json=child, headers=headers).json()["report_id"]

    cards = hist_client.get("/api/compliance-history/notebooks", headers=headers).json()["notebooks"]
    legacy = [c for c in cards if c["id"] == root_id]
    assert len(legacy) == 1 and legacy[0]["document_count"] == 2

    assert hist_client.delete(f"/api/compliance-history/{root_id}", headers=headers).status_code == 200
    assert pg_service.query_one("SELECT id FROM compliance_history WHERE id = %s", (child_id,)) is None
    assert pg_service.query_one("SELECT id FROM compliance_history WHERE id = %s", (root_id,)) is None

    _clean_reports(uid)
