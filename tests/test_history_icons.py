"""Tests for workspace Lucide icon integration in history router.

Covers:
- GET /compliance-history/notebooks/suggest-icon endpoint
- POST /compliance-history/notebooks (empty workspace) with auto-infer & manual icon
- PUT /compliance-history/notebooks/{id} (update workspace) with icon
- POST /compliance-history (first upload mints document-rooted workspace with icon)
- GET /compliance-history/notebooks (icon in card payload & ID deduplication)
- GET /compliance-history/{id}/documents (returns notebook_icon)
"""
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import pg_service

# Prevent auth.init_db() from connecting to real database at import time
pg_service.query_one = lambda sql, params=None: {"1": 1}
pg_service.execute = lambda sql, params=None: 1
pg_service.execute_many = lambda sql, seq: None

import history
from auth import get_current_user


@pytest.fixture
def mock_db(monkeypatch):
    """Mock pg_service calls to provide in-memory tracking without requiring live Postgres."""
    executed = []
    queries = []

    def fake_execute(sql, params=None):
        executed.append((sql, params))
        return 1

    def fake_query(sql, params=None):
        queries.append((sql, params))
        return []

    def fake_query_one(sql, params=None):
        queries.append((sql, params))
        return None

    class FakeConn:
        def cursor(self):
            return self
        def execute(self, sql, params=None):
            executed.append((sql, params))
        def executemany(self, sql, seq):
            for p in seq:
                executed.append((sql, p))
        def fetchall(self):
            return []
        def fetchone(self):
            return None
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr(pg_service, "execute", fake_execute)
    monkeypatch.setattr(pg_service, "query", fake_query)
    monkeypatch.setattr(pg_service, "query_one", fake_query_one)
    monkeypatch.setattr(pg_service, "get_conn", lambda: FakeConn())

    return {
        "executed": executed,
        "queries": queries,
        "set_query": lambda fn: monkeypatch.setattr(pg_service, "query", fn),
        "set_query_one": lambda fn: monkeypatch.setattr(pg_service, "query_one", fn),
        "set_execute": lambda fn: monkeypatch.setattr(pg_service, "execute", fn),
    }


@pytest.fixture
def client(monkeypatch):
    """TestClient over history router with stubbed embed and mock user."""
    monkeypatch.setattr(history, "embed_documents", lambda texts: [[] for _ in texts])

    app = FastAPI()
    app.include_router(history.router, prefix="/api")

    # Mock user auth dependency
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "test-user-uuid",
        "username": "tester",
        "role": "pengguna",
    }
    return TestClient(app)


# -------------------------------------------------------------------------
# 1. suggest-icon endpoint tests
# -------------------------------------------------------------------------

def test_suggest_icon_canonical_patterns(client):
    cases = [
        ("PKS Lintasarta 2026", "handshake"),
        ("Berita Acara Serah Terima", "clipboard-check"),
        ("Addendum Kontrak Kerjasama", "file-plus"),
        ("Data Sharing Agreement", "share-2"),
        ("Data Processing Agreement", "shield-check"),
        ("Perjanjian Kerahasiaan (NDA)", "lock"),
        ("SLA Datacenter Service", "gauge"),
        ("Statement of Work Phase 1", "list-checks"),
        ("Nota Kesepahaman B2B", "file-signature"),
        ("Purchase Order Perangkat", "shopping-cart"),
        ("Surat Perintah Kerja Mandiri", "file-text"),
        ("Reseller Agreement PT ABC", "store"),
    ]
    for name, expected_icon in cases:
        res = client.get("/api/compliance-history/notebooks/suggest-icon", params={"name": name})
        assert res.status_code == 200
        data = res.json()
        assert data.get("icon") == expected_icon, f"Failed for {name}: got {data.get('icon')}"


def test_suggest_icon_novel_and_empty(client):
    # Unmatched / novel should fallback to 'folder'
    res = client.get("/api/compliance-history/notebooks/suggest-icon", params={"name": "Random Project"})
    assert res.status_code == 200
    assert res.json() == {"icon": "folder"}

    # Blank / missing name query param should fallback to 'folder'
    res_empty = client.get("/api/compliance-history/notebooks/suggest-icon")
    assert res_empty.status_code == 200
    assert res_empty.json() == {"icon": "folder"}

    res_blank = client.get("/api/compliance-history/notebooks/suggest-icon", params={"name": "   "})
    assert res_blank.status_code == 200
    assert res_blank.json() == {"icon": "folder"}


# -------------------------------------------------------------------------
# 2. create_empty_notebook tests
# -------------------------------------------------------------------------

def test_create_empty_notebook_inferred_icon(client, mock_db):
    res = client.post("/api/compliance-history/notebooks", json={
        "name": "PKS Pengadaan Jaringan",
        "description": "Deskripsi proyek",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "PKS Pengadaan Jaringan"
    assert data["description"] == "Deskripsi proyek"
    assert data["icon"] == "handshake"
    assert uuid.UUID(data["notebook_id"])

    # Verify SQL execution recorded icon
    assert len(mock_db["executed"]) >= 1
    sql, params = mock_db["executed"][-1]
    assert "INSERT INTO notebooks" in sql
    assert "icon" in sql
    assert "handshake" in params


def test_create_empty_notebook_manual_valid_icon(client, mock_db):
    res = client.post("/api/compliance-history/notebooks", json={
        "name": "Proyek Bebas",
        "icon": "shield-check",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Proyek Bebas"
    assert data["icon"] == "shield-check"

    sql, params = mock_db["executed"][-1]
    assert "INSERT INTO notebooks" in sql
    assert "shield-check" in params


def test_create_empty_notebook_manual_invalid_icon_fallback(client, mock_db):
    res = client.post("/api/compliance-history/notebooks", json={
        "name": "Proyek Bebas",
        "icon": "invalid<script>alert(1)</script>",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["icon"] == "folder"

    sql, params = mock_db["executed"][-1]
    assert "folder" in params


# -------------------------------------------------------------------------
# 3. update_notebook tests
# -------------------------------------------------------------------------

def test_update_notebook_icon_existing_notebooks_row(client, mock_db):
    nb_id = str(uuid.uuid4())
    # Mock existing row in notebooks table
    mock_db["set_query_one"](lambda sql, params: {
        "id": nb_id,
        "name": "Old Name",
        "description": "Old Desc",
        "icon": "folder",
    } if "FROM notebooks" in sql else None)

    res = client.put(f"/api/compliance-history/notebooks/{nb_id}", json={
        "name": "New Name",
        "icon": "lock",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["name"] == "New Name"
    assert data["icon"] == "lock"

    # Verify UPDATE statement included icon
    update_calls = [c for c in mock_db["executed"] if "UPDATE notebooks SET" in c[0]]
    assert len(update_calls) >= 1
    sql, params = update_calls[-1]
    assert "icon = %s" in sql
    assert "lock" in params


def test_update_notebook_icon_legacy_history_row(client, mock_db):
    nb_id = str(uuid.uuid4())
    def fake_query_one(sql, params):
        if "FROM notebooks WHERE id = %s AND user_id = %s" in sql:
            return None
        if "FROM compliance_history WHERE id = %s AND user_id = %s" in sql:
            return {"id": nb_id, "company_name": "Old Company"}
        if "FROM notebooks WHERE id = %s" in sql:
            return None
        return None

    mock_db["set_query_one"](fake_query_one)

    res = client.put(f"/api/compliance-history/notebooks/{nb_id}", json={
        "name": "Updated Legacy Name",
        "icon": "gauge",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["name"] == "Updated Legacy Name"
    assert data["icon"] == "gauge"

    # Verify INSERT INTO notebooks occurred with icon
    insert_calls = [c for c in mock_db["executed"] if "INSERT INTO notebooks" in c[0]]
    assert len(insert_calls) >= 1
    sql, params = insert_calls[-1]
    assert "icon" in sql
    assert "gauge" in params


# -------------------------------------------------------------------------
# 4. save_compliance_report (upload) creates notebook with inferred icon
# -------------------------------------------------------------------------

def test_save_compliance_report_creates_notebook_with_inferred_icon(client, mock_db):
    res = client.post("/api/compliance-history", json={
        "filename": "pks_telco_2026.pdf",
        "company_name": "PKS Layanan Telekomunikasi",
        "expiration_date": "2026-12-31",
        "results": {
            "summary": {"jenis_dokumen": "PKS"},
            "results": [],
        },
    })
    assert res.status_code == 200
    data = res.json()
    assert data["notebook_id"]
    assert data["report_id"]
    assert data["notebook_id"] == data["report_id"]

    # Verify notebooks table received insert with inferred icon "handshake"
    insert_nb = [c for c in mock_db["executed"] if "INSERT INTO notebooks" in c[0]]
    assert len(insert_nb) >= 1
    sql, params = insert_nb[0]
    assert "icon" in sql
    assert "handshake" in params


# -------------------------------------------------------------------------
# 5. get_compliance_notebooks returns icon & deduplicates
# -------------------------------------------------------------------------

def test_get_compliance_notebooks_returns_icon_and_deduplicates(client, mock_db):
    ws_1 = str(uuid.uuid4())
    ws_2 = str(uuid.uuid4())

    def fake_query(sql, params):
        if "FROM compliance_history h" in sql:
            # Document-rooted workspace ws_1
            return [{
                "id": ws_1,
                "filename": "pks_doc.pdf",
                "company_name": "PT Alpha PKS",
                "expiration_date": "2026-12-31",
                "created_at": "2026-11-10 10:00:00",
                "document_count": 2,
                "modified_at": "2026-11-10 10:05:00",
                "last_accessed_at": "2026-11-10 10:05:00",
                "description": "Workspace note",
                "icon": "handshake",
                "recent_documents": ["pks_doc.pdf", "doc2.pdf"],
            }]
        elif "FROM notebooks n" in sql:
            # notebooks query returns ws_1 (duplicate) and ws_2 (empty workspace)
            return [
                {
                    "id": ws_1,
                    "filename": "",
                    "company_name": "PT Alpha PKS",
                    "expiration_date": None,
                    "created_at": "2026-11-10 10:00:00",
                    "document_count": 2,
                    "modified_at": "2026-11-10 10:05:00",
                    "last_accessed_at": "2026-11-10 10:05:00",
                    "description": "Workspace note",
                    "icon": "handshake",
                    "recent_documents": [],
                },
                {
                    "id": ws_2,
                    "filename": "",
                    "company_name": "Empty SOW Workspace",
                    "expiration_date": None,
                    "created_at": "2026-11-10 09:00:00",
                    "document_count": 0,
                    "modified_at": None,
                    "last_accessed_at": "2026-11-10 09:00:00",
                    "description": "",
                    "icon": "list-checks",
                    "recent_documents": [],
                },
            ]
        return []

    mock_db["set_query"](fake_query)

    res = client.get("/api/compliance-history/notebooks")
    assert res.status_code == 200
    cards = res.json()["notebooks"]

    # Deduplication check: should have exactly 2 cards, not 3
    assert len(cards) == 2
    ids = [c["id"] for c in cards]
    assert ids.count(ws_1) == 1
    assert ids.count(ws_2) == 1

    # Check icons are present
    card_map = {c["id"]: c for c in cards}
    assert card_map[ws_1]["icon"] == "handshake"
    assert card_map[ws_2]["icon"] == "list-checks"


# -------------------------------------------------------------------------
# 6. get_notebook_documents returns notebook_icon
# -------------------------------------------------------------------------

def test_get_notebook_documents_returns_notebook_icon(client, mock_db):
    report_id = str(uuid.uuid4())
    notebook_id = str(uuid.uuid4())

    def fake_query_one(sql, params):
        if "FROM compliance_history WHERE id = %s AND user_id = %s" in sql:
            return {"notebook_id": notebook_id}
        if "FROM notebooks WHERE id = %s" in sql:
            return {
                "name": "DPA Project",
                "description": "Notes",
                "icon": "shield-check",
            }
        return None

    def fake_query(sql, params):
        if "FROM compliance_history" in sql:
            return [{
                "id": report_id,
                "filename": "dpa_doc.pdf",
                "company_name": "PT Vendor",
                "review_status": "draft",
                "expiration_date": "2027-01-01",
                "created_at": "2026-11-10 11:00:00",
            }]
        return []

    mock_db["set_query_one"](fake_query_one)
    mock_db["set_query"](fake_query)

    res = client.get(f"/api/compliance-history/{report_id}/documents")
    assert res.status_code == 200
    data = res.json()
    assert "documents" in data
    assert len(data["documents"]) == 1
    assert data.get("notebook_name") == "DPA Project"
    assert data.get("notebook_icon") == "shield-check"
