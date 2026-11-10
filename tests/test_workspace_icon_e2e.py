"""End-to-end tests for workspace Lucide icons.

Simulates complete user and client workflows:
1. Workspace creation with auto-inference (e.g. "PKS Pengadaan Jaringan Fiber" -> icon == 'handshake').
2. Workspace creation with manual override (e.g. name = "PKS Pengadaan Jaringan", icon = "shield-check" -> icon == 'shield-check').
3. Workspace rename updating only icon or both name and icon (PUT /compliance-history/notebooks/{id}).
4. GET /compliance-history/notebooks returning deduplicated list with icon.
5. Contract upload creating workspace with inferred icon.
6. suggest-icon route returning instant canonical prediction.
7. Complete end-to-end user lifecycle flow.
"""
import datetime
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import pg_service

# Stub auth DB initialization at import time
pg_service.query_one = lambda sql, params=None: {"1": 1}
pg_service.execute = lambda sql, params=None: 1
pg_service.execute_many = lambda sql, seq: None

import history
from auth import get_current_user


class StatefulMockDB:
    """In-memory realistic database simulator for workspace and history tables."""

    def __init__(self):
        self.notebooks = {}  # id -> row dict
        self.compliance_history = {}  # id -> row dict
        self.chunks = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)

    def execute(self, sql, params=None):
        params = params or ()
        sql_upper = sql.strip().upper()

        if "INSERT INTO NOTEBOOKS" in sql_upper:
            if "ON CONFLICT (ID) DO NOTHING" in sql_upper:
                # (id, user_id, name, icon)
                nid, uid, name, icon = params[0], params[1], params[2], params[3]
                if nid not in self.notebooks:
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.notebooks[nid] = {
                        "id": nid,
                        "user_id": uid,
                        "name": name,
                        "description": "",
                        "icon": icon,
                        "created_at": now_str,
                        "last_accessed_at": now_str,
                    }
            elif "ON CONFLICT (ID) DO UPDATE" in sql_upper:
                nid, uid, name, icon = params[0], params[1], params[2], params[3]
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if nid in self.notebooks:
                    self.notebooks[nid]["last_accessed_at"] = now_str
                else:
                    self.notebooks[nid] = {
                        "id": nid,
                        "user_id": uid,
                        "name": name,
                        "description": "",
                        "icon": icon,
                        "created_at": now_str,
                        "last_accessed_at": now_str,
                    }
            else:
                # (id, user_id, name, description, icon)
                nid, uid, name, desc, icon = params[0], params[1], params[2], params[3], params[4]
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.notebooks[nid] = {
                    "id": nid,
                    "user_id": uid,
                    "name": name,
                    "description": desc,
                    "icon": icon,
                    "created_at": now_str,
                    "last_accessed_at": now_str,
                }
            return 1

        elif "UPDATE NOTEBOOKS SET" in sql_upper:
            if "LAST_ACCESSED_AT = NOW()" in sql_upper and "NAME =" not in sql_upper:
                nid = params[0]
                if nid in self.notebooks:
                    self.notebooks[nid]["last_accessed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                name, desc, icon, nid = params[0], params[1], params[2], params[3]
                if nid in self.notebooks:
                    self.notebooks[nid]["name"] = name
                    self.notebooks[nid]["description"] = desc
                    self.notebooks[nid]["icon"] = icon
                    self.notebooks[nid]["last_accessed_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return 1

        elif "INSERT INTO COMPLIANCE_HISTORY" in sql_upper:
            rid, uid, fname, cname, exp, rjson, nid = params
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.compliance_history[rid] = {
                "id": rid,
                "user_id": uid,
                "filename": fname,
                "company_name": cname,
                "expiration_date": exp,
                "results_json": rjson,
                "notebook_id": nid,
                "review_status": "draft",
                "created_at": now_str,
            }
            return 1

        elif "UPDATE COMPLIANCE_HISTORY SET COMPANY_NAME =" in sql_upper:
            cname, nid = params[0], params[1]
            for doc in self.compliance_history.values():
                if doc["id"] == nid or doc["notebook_id"] == nid:
                    doc["company_name"] = cname
            return 1

        elif "INSERT INTO CHUNKS" in sql_upper:
            self.chunks.append(params)
            return 1

        return 1

    def query_one(self, sql, params=None):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def query(self, sql, params=None):
        params = params or ()
        sql_upper = sql.strip().upper()

        # SELECT id, name, description, icon FROM notebooks WHERE id = %s AND user_id = %s
        if "FROM NOTEBOOKS" in sql_upper and "WHERE ID = %S AND USER_ID = %S" in sql_upper:
            nid, uid = params[0], params[1]
            nb = self.notebooks.get(nid)
            return [dict(nb)] if (nb and nb["user_id"] == uid) else []

        # SELECT id, icon FROM notebooks WHERE id = %s
        # or SELECT name, description, icon FROM notebooks WHERE id = %s
        if "FROM NOTEBOOKS" in sql_upper and "WHERE ID = %S" in sql_upper:
            nid = params[0]
            nb = self.notebooks.get(nid)
            return [dict(nb)] if nb else []

        # SELECT id, company_name FROM compliance_history WHERE id = %s AND user_id = %s
        if "FROM COMPLIANCE_HISTORY" in sql_upper and "WHERE ID = %S AND USER_ID = %S" in sql_upper:
            rid, uid = params[0], params[1]
            doc = self.compliance_history.get(rid)
            return [dict(doc)] if (doc and doc["user_id"] == uid) else []

        # SELECT notebook_id FROM compliance_history WHERE id = %s AND user_id = %s
        if "SELECT NOTEBOOK_ID FROM COMPLIANCE_HISTORY" in sql_upper and "WHERE ID = %S AND USER_ID = %S" in sql_upper:
            rid, uid = params[0], params[1]
            doc = self.compliance_history.get(rid)
            return [{"notebook_id": doc["notebook_id"]}] if (doc and doc["user_id"] == uid) else []

        # SELECT id, notebook_id, company_name, filename FROM compliance_history WHERE id = %s
        if "FROM COMPLIANCE_HISTORY" in sql_upper and "WHERE ID = %S" in sql_upper:
            rid = params[0]
            doc = self.compliance_history.get(rid)
            return [dict(doc)] if doc else []

        # SELECT h.id, h.filename, h.company_name, ... FROM compliance_history h LEFT JOIN notebooks n ON n.id = h.notebook_id WHERE h.user_id = %s AND h.notebook_id = h.id ORDER BY h.created_at DESC
        if "FROM COMPLIANCE_HISTORY H" in sql_upper and "H.NOTEBOOK_ID = H.ID" in sql_upper:
            uid = params[0]
            results = []
            for doc in self.compliance_history.values():
                if doc["user_id"] == uid and doc["notebook_id"] == doc["id"]:
                    nb = self.notebooks.get(doc["id"], {})
                    cnt = sum(1 for d in self.compliance_history.values() if d.get("notebook_id") == doc["id"])
                    recent_docs = [
                        d["filename"] for d in self.compliance_history.values() if d.get("notebook_id") == doc["id"]
                    ]
                    results.append({
                        "id": doc["id"],
                        "filename": doc["filename"],
                        "company_name": doc["company_name"],
                        "expiration_date": doc["expiration_date"],
                        "created_at": doc["created_at"],
                        "document_count": cnt,
                        "modified_at": doc["created_at"],
                        "last_accessed_at": nb.get("last_accessed_at", doc["created_at"]),
                        "description": nb.get("description", ""),
                        "icon": nb.get("icon", "folder"),
                        "recent_documents": recent_docs[:3],
                    })
            return results

        # SELECT n.id, '' AS filename, n.name AS company_name, ... FROM notebooks n WHERE n.user_id = %s
        if "FROM NOTEBOOKS N WHERE N.USER_ID = %S" in sql_upper:
            uid = params[0]
            results = []
            for nb in self.notebooks.values():
                if nb["user_id"] == uid:
                    cnt = sum(1 for d in self.compliance_history.values() if d.get("notebook_id") == nb["id"])
                    recent = [
                        d["filename"] for d in self.compliance_history.values() if d.get("notebook_id") == nb["id"]
                    ]
                    results.append({
                        "id": nb["id"],
                        "filename": "",
                        "company_name": nb["name"],
                        "expiration_date": None,
                        "created_at": nb["created_at"],
                        "document_count": cnt,
                        "modified_at": nb.get("last_accessed_at"),
                        "last_accessed_at": nb.get("last_accessed_at", nb["created_at"]),
                        "description": nb.get("description", ""),
                        "icon": nb.get("icon", "folder"),
                        "recent_documents": recent[:3],
                    })
            return results

        # SELECT id, filename, company_name, ... FROM compliance_history WHERE notebook_id = %s
        if "FROM COMPLIANCE_HISTORY" in sql_upper and "NOTEBOOK_ID = %S" in sql_upper:
            nid = params[0]
            return [dict(d) for d in self.compliance_history.values() if d["notebook_id"] == nid]

        return []


@pytest.fixture
def stateful_db(monkeypatch):
    """Provide a fresh isolated stateful database instance for each test."""
    db = StatefulMockDB()
    monkeypatch.setattr(pg_service, "execute", db.execute)
    monkeypatch.setattr(pg_service, "query", db.query)
    monkeypatch.setattr(pg_service, "query_one", db.query_one)
    monkeypatch.setattr(pg_service, "get_conn", lambda: db)
    return db


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with stubbed embeddings and mock auth."""
    monkeypatch.setattr(history, "embed_documents", lambda texts: [[] for _ in texts])

    app = FastAPI()
    app.include_router(history.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "e2e-user-12345",
        "username": "tester",
        "role": "pengguna",
    }
    return TestClient(app)


# -------------------------------------------------------------------------
# Test Cases
# -------------------------------------------------------------------------

def test_suggest_icon_canonical_predictions(client):
    """Verification 1: suggest-icon route returns instant canonical prediction."""
    cases = [
        ("PKS Pengadaan Jaringan Fiber Optic", "handshake"),
        ("BAST Serah Terima Perangkat Server", "clipboard-check"),
        ("Addendum Kontrak Lisensi Software", "file-plus"),
        ("Perjanjian Kerahasiaan (NDA) Mitra", "lock"),
        ("SLA Cloud Infrastructure 99.9%", "gauge"),
        ("Statement of Work (SOW) Migrasi Cloud", "list-checks"),
        ("Purchase Order (PO) Switch Core", "shopping-cart"),
        ("Data Processing Agreement (DPA)", "shield-check"),
        ("Data Sharing Agreement Pelanggan", "share-2"),
        ("Nota Kesepahaman (MoU) Riset AI", "file-signature"),
        ("Surat Perintah Kerja (SPK) Maintenance", "file-text"),
        ("Perjanjian Reseller Distribusi Produk", "store"),
        ("General Non-Contract Project", "folder"),
        ("", "folder"),
        ("   ", "folder"),
    ]
    for name, expected in cases:
        res = client.get("/api/compliance-history/notebooks/suggest-icon", params={"name": name})
        assert res.status_code == 200
        assert res.json() == {"icon": expected}, f"Failed for {name}"


def test_workspace_creation_auto_inference(client, stateful_db):
    """Verification 2: Workspace creation with auto-inference (e.g. PKS -> handshake)."""
    res = client.post("/api/compliance-history/notebooks", json={
        "name": "PKS Pengadaan Jaringan Fiber",
        "description": "Pengadaan infrastruktur fiber optic antar data center",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "PKS Pengadaan Jaringan Fiber"
    assert data["description"] == "Pengadaan infrastruktur fiber optic antar data center"
    assert data["icon"] == "handshake"
    nb_id = data["notebook_id"]
    assert uuid.UUID(nb_id)

    # Verify stateful database row
    assert nb_id in stateful_db.notebooks
    stored = stateful_db.notebooks[nb_id]
    assert stored["icon"] == "handshake"
    assert stored["name"] == "PKS Pengadaan Jaringan Fiber"


def test_workspace_creation_manual_override(client, stateful_db):
    """Verification 3: Workspace creation with manual override (e.g. name = PKS, icon = shield-check)."""
    res = client.post("/api/compliance-history/notebooks", json={
        "name": "PKS Pengadaan Jaringan Fiber",
        "description": "Custom security-focused workspace",
        "icon": "shield-check",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "PKS Pengadaan Jaringan Fiber"
    assert data["icon"] == "shield-check"  # Manual override wins over inferred 'handshake'

    nb_id = data["notebook_id"]
    assert stateful_db.notebooks[nb_id]["icon"] == "shield-check"


def test_workspace_creation_invalid_icon_fallback(client, stateful_db):
    """Verification 4: Workspace creation with uncurated/malformed icon falls back to 'folder'."""
    res = client.post("/api/compliance-history/notebooks", json={
        "name": "General Project Workspace",
        "icon": "malicious<script>alert(1)</script>",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["icon"] == "folder"

    nb_id = data["notebook_id"]
    assert stateful_db.notebooks[nb_id]["icon"] == "folder"


def test_workspace_rename_and_icon_update(client, stateful_db):
    """Verification 5: Workspace rename updating only icon, or both name and icon."""
    # Create initial workspace
    create_res = client.post("/api/compliance-history/notebooks", json={
        "name": "Initial Workspace Name",
        "icon": "folder",
    })
    assert create_res.status_code == 200
    nb_id = create_res.json()["notebook_id"]

    # 1. Update ONLY icon
    res_icon_only = client.put(f"/api/compliance-history/notebooks/{nb_id}", json={
        "icon": "server",
    })
    assert res_icon_only.status_code == 200
    data1 = res_icon_only.json()
    assert data1["status"] == "ok"
    assert data1["icon"] == "server"
    assert data1["name"] == "Initial Workspace Name"
    assert stateful_db.notebooks[nb_id]["icon"] == "server"

    # 2. Update BOTH name and icon
    res_both = client.put(f"/api/compliance-history/notebooks/{nb_id}", json={
        "name": "BAST Handover System 2026",
        "icon": "clipboard-check",
    })
    assert res_both.status_code == 200
    data2 = res_both.json()
    assert data2["status"] == "ok"
    assert data2["name"] == "BAST Handover System 2026"
    assert data2["icon"] == "clipboard-check"
    assert stateful_db.notebooks[nb_id]["name"] == "BAST Handover System 2026"
    assert stateful_db.notebooks[nb_id]["icon"] == "clipboard-check"

    # 3. Update ONLY name (icon preserved)
    res_name_only = client.put(f"/api/compliance-history/notebooks/{nb_id}", json={
        "name": "BAST Handover Final Approved",
    })
    assert res_name_only.status_code == 200
    data3 = res_name_only.json()
    assert data3["name"] == "BAST Handover Final Approved"
    assert data3["icon"] == "clipboard-check"  # Preserved
    assert stateful_db.notebooks[nb_id]["icon"] == "clipboard-check"


def test_contract_upload_mints_workspace_with_inferred_icon(client, stateful_db):
    """Verification 6: Contract upload creating document-rooted workspace with inferred icon."""
    res = client.post("/api/compliance-history", json={
        "filename": "Perjanjian_Kerahasiaan_Lintasarta.pdf",
        "company_name": "Non-Disclosure Agreement (NDA) Mitra Vendor",
        "expiration_date": "2027-12-31",
        "results": {
            "summary": {"jenis_dokumen": "NDA"},
            "results": [
                {"pasal": "Pasal 1", "isi_pasal": "Informasi rahasia wajib dijaga kerahasiaannya."}
            ],
        },
    })
    assert res.status_code == 200
    data = res.json()
    assert data["report_id"]
    assert data["notebook_id"]
    assert data["report_id"] == data["notebook_id"]

    ws_id = data["notebook_id"]
    # Check that notebooks table minted a row with inferred icon 'lock'
    assert ws_id in stateful_db.notebooks
    assert stateful_db.notebooks[ws_id]["icon"] == "lock"


def test_get_notebooks_deduplicated_list_with_icons(client, stateful_db):
    """Verification 7: GET /compliance-history/notebooks returns deduplicated list with icons."""
    # Workspace 1: Empty workspace created with 'gauge'
    r1 = client.post("/api/compliance-history/notebooks", json={
        "name": "SLA Infrastructure Monitoring",
        "icon": "gauge",
    })
    ws1_id = r1.json()["notebook_id"]

    # Workspace 2: Root upload created with 'lock'
    r2 = client.post("/api/compliance-history", json={
        "filename": "nda_alpha.pdf",
        "company_name": "Perjanjian Kerahasiaan (NDA) Alpha",
        "results": {"summary": {}, "results": []},
    })
    ws2_id = r2.json()["notebook_id"]

    # Add a second document into Workspace 2 (passes notebook_id = ws2_id)
    r3 = client.post("/api/compliance-history", json={
        "filename": "nda_appendix.pdf",
        "company_name": "Perjanjian Kerahasiaan (NDA) Alpha",
        "notebook_id": ws2_id,
        "results": {"summary": {}, "results": []},
    })
    assert r3.status_code == 200

    # Query all notebooks
    res = client.get("/api/compliance-history/notebooks")
    assert res.status_code == 200
    cards = res.json()["notebooks"]

    # Deduplication assertion: exactly 2 cards for ws1 and ws2
    assert len(cards) == 2
    card_map = {c["id"]: c for c in cards}
    assert ws1_id in card_map
    assert ws2_id in card_map

    # Check icons and document counts
    assert card_map[ws1_id]["icon"] == "gauge"
    assert card_map[ws1_id]["document_count"] == 0

    assert card_map[ws2_id]["icon"] == "lock"
    assert card_map[ws2_id]["document_count"] == 2


def test_complete_user_lifecycle_flow(client, stateful_db):
    """Verification 8: Full end-to-end lifecycle simulation of user and client interactions.

    Lifecycle:
    1. User opens Create Workspace modal, types name: "PKS Kemitraan Fiber".
    2. Frontend fetches suggestion from GET /suggest-icon -> gets 'handshake'.
    3. User clicks Create -> POST /notebooks creates workspace with icon 'handshake'.
    4. User opens workspace -> GET /notebooks/{id}/documents verifies header crumb icon 'handshake'.
    5. User opens Edit Workspace modal, picks 'briefcase' -> PUT updates icon to 'briefcase'.
    6. User uploads contract into workspace -> POST /compliance-history with notebook_id.
    7. User returns to home -> GET /notebooks returns card with 'briefcase' and document_count = 1.
    """
    # 1 & 2. Suggest icon
    sug_res = client.get("/api/compliance-history/notebooks/suggest-icon", params={
        "name": "PKS Kemitraan Fiber"
    })
    assert sug_res.status_code == 200
    assert sug_res.json()["icon"] == "handshake"

    # 3. Create workspace with inferred icon
    create_res = client.post("/api/compliance-history/notebooks", json={
        "name": "PKS Kemitraan Fiber",
        "description": "Kerjasama fiber optik nasional",
    })
    assert create_res.status_code == 200
    ws = create_res.json()
    ws_id = ws["notebook_id"]
    assert ws["icon"] == "handshake"

    # 4. Open workspace detail
    doc_res = client.get(f"/api/compliance-history/{ws_id}/documents")
    assert doc_res.status_code == 200
    doc_data = doc_res.json()
    assert doc_data["notebook_name"] == "PKS Kemitraan Fiber"
    assert doc_data["notebook_icon"] == "handshake"
    assert len(doc_data["documents"]) == 0

    # 5. User renames/updates icon to 'briefcase'
    edit_res = client.put(f"/api/compliance-history/notebooks/{ws_id}", json={
        "icon": "briefcase",
    })
    assert edit_res.status_code == 200
    assert edit_res.json()["icon"] == "briefcase"

    # 6. User uploads document into this existing workspace
    upload_res = client.post("/api/compliance-history", json={
        "filename": "kontrak_fiber_2026.pdf",
        "company_name": "PKS Kemitraan Fiber",
        "notebook_id": ws_id,
        "results": {
            "summary": {"jenis_dokumen": "PKS"},
            "results": [{"pasal": "Pasal 1", "isi_pasal": "Kerjasama operasional jaringan fiber."}],
        },
    })
    assert upload_res.status_code == 200
    assert upload_res.json()["notebook_id"] == ws_id

    # 7. Home list returns the updated icon and incremented document count
    home_res = client.get("/api/compliance-history/notebooks")
    assert home_res.status_code == 200
    cards = home_res.json()["notebooks"]
    matching_card = next(c for c in cards if c["id"] == ws_id)
    assert matching_card["icon"] == "briefcase"
    assert matching_card["document_count"] == 1
