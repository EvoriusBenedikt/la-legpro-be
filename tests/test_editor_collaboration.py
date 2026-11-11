import pytest
from fastapi import HTTPException
from routers.editor import (
    verify_report_access,
    get_collaborators,
    invite_collaborator,
    remove_collaborator,
    editor_heartbeat,
    list_editor_versions,
    create_editor_version,
    restore_editor_version,
    CollaboratorInviteRequest,
    EditorHeartbeatRequest,
    CreateVersionRequest,
)


@pytest.fixture(autouse=True)
def skip_table_ensure(monkeypatch):
    monkeypatch.setattr("routers.editor._ensure_editor_tables", lambda: None)


def test_verify_report_access_owner(monkeypatch):
    def mock_query_one(sql, params):
        if "FROM compliance_history" in sql:
            return {
                "id": params[0],
                "filename": "kontrak.pdf",
                "user_id": "owner-123",
                "notebook_id": "nb-456",
            }
        return None

    from services import pg_service
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)

    res = verify_report_access("doc-1", "owner-123")
    assert res["id"] == "doc-1"
    assert res["is_owner"] is True
    assert res["color_index"] == 0
    assert res["owner_id"] == "owner-123"


def test_verify_report_access_collaborator(monkeypatch):
    def mock_query_one(sql, params):
        if "FROM compliance_history" in sql:
            return {
                "id": params[0],
                "filename": "kontrak.pdf",
                "user_id": "owner-123",
                "notebook_id": "nb-456",
            }
        if "FROM workspace_collaborators" in sql:
            if params[0] == "nb-456" and params[1] == "collab-789":
                return {"id": "c-1", "role": "editor", "color_index": 2}
        return None

    from services import pg_service
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)

    res = verify_report_access("doc-1", "collab-789")
    assert res["id"] == "doc-1"
    assert res["is_owner"] is False
    assert res["color_index"] == 2
    assert res["owner_id"] == "owner-123"


def test_verify_report_access_outsider_raises_404(monkeypatch):
    def mock_query_one(sql, params):
        if "FROM compliance_history" in sql:
            return {
                "id": params[0],
                "filename": "kontrak.pdf",
                "user_id": "owner-123",
                "notebook_id": "nb-456",
            }
        return None

    from services import pg_service
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)

    with pytest.raises(HTTPException) as exc_info:
        verify_report_access("doc-1", "stranger-000")
    assert exc_info.value.status_code == 404


def test_collaborators_list_and_invite_flow(monkeypatch):
    fake_users = {
        "owner-1": {"id": "owner-1", "username": "boss", "email": "boss@lintasarta.co.id"},
        "collab-1": {"id": "collab-1", "username": "collab_u", "email": "collab@lintasarta.co.id"},
        "newbie-1": {"id": "newbie-1", "username": "newbie", "email": "newbie@lintasarta.co.id"},
    }
    collabs_db = [
        {"id": "c-1", "notebook_id": "nb-1", "user_id": "collab-1", "invited_by": "owner-1", "role": "editor", "color_index": 1}
    ]

    def mock_query_one(sql, params):
        if "FROM compliance_history" in sql:
            return {"id": params[0], "filename": "kontrak.pdf", "user_id": "owner-1", "notebook_id": "nb-1"}
        if "FROM users WHERE id = %s" in sql:
            return fake_users.get(params[0])
        if "FROM users WHERE" in sql:
            # Identifier query: email, username, or id
            val = str(params[0]).lower()
            for u in fake_users.values():
                if u["email"].lower() == val or u["username"].lower() == val or u["id"] == params[2]:
                    return u
            return None
        if "FROM workspace_collaborators" in sql and "SELECT count(*)" not in sql:
            for c in collabs_db:
                if c["notebook_id"] == params[0] and c["user_id"] == params[1]:
                    return c
            return None
        if "SELECT count(*) FROM workspace_collaborators" in sql:
            return {"count": len(collabs_db)}
        return None

    def mock_query(sql, params=None):
        if "FROM workspace_collaborators" in sql:
            out = []
            for c in collabs_db:
                u = fake_users.get(c["user_id"])
                out.append({
                    "user_id": c["user_id"],
                    "username": u["username"] if u else "unknown",
                    "email": u["email"] if u else "",
                    "color_index": c["color_index"],
                    "role": c["role"],
                })
            return out
        return []

    def mock_execute(sql, params=None):
        if "INSERT INTO workspace_collaborators" in sql:
            # (id, notebook_id, user_id, invited_by, role, color_index)
            collabs_db.append({
                "id": params[0],
                "notebook_id": params[1],
                "user_id": params[2],
                "invited_by": params[3],
                "role": params[4],
                "color_index": params[5],
            })
            return 1
        return 0

    from services import pg_service
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)
    monkeypatch.setattr(pg_service, "query", mock_query)
    monkeypatch.setattr(pg_service, "execute", mock_execute)

    # 1. GET collaborators as owner
    resp = get_collaborators("rep-1", current_user=fake_users["owner-1"])
    assert resp["is_owner"] is True
    assert resp["my_color_index"] == 0
    assert len(resp["collaborators"]) == 2  # owner + collab-1
    assert resp["collaborators"][0]["is_owner"] is True
    assert resp["collaborators"][1]["color_index"] == 1

    # 2. Invite newbie by email
    inv_req = CollaboratorInviteRequest(identifier="newbie@lintasarta.co.id")
    inv_resp = invite_collaborator("rep-1", inv_req, current_user=fake_users["owner-1"])
    assert len(inv_resp["collaborators"]) == 3
    newbie_entry = [c for c in inv_resp["collaborators"] if c["user_id"] == "newbie-1"][0]
    assert newbie_entry["color_index"] == 2  # Next available index

    # 3. Non-owner cannot invite
    with pytest.raises(HTTPException) as exc:
        invite_collaborator("rep-1", inv_req, current_user=fake_users["collab-1"])
    assert exc.value.status_code == 403


def test_editor_heartbeat_and_presence(monkeypatch):
    presence_db = {}
    draft_db = {
        "content_html": "<p>Teks diedit</p>",
        "version": 4,
        "updated_at": "2026-11-11T12:00:00.000000Z",
    }

    def mock_query_one(sql, params):
        if "FROM compliance_history" in sql:
            return {"id": params[0], "filename": "kontrak.pdf", "user_id": "u-owner", "notebook_id": "nb-1"}
        if "FROM document_editor_drafts" in sql:
            return draft_db
        return None

    def mock_query(sql, params=None):
        if "FROM workspace_presence" in sql:
            return list(presence_db.values())
        return []

    def mock_execute(sql, params=None):
        if "INSERT INTO workspace_presence" in sql:
            # (report_id, user_id, username, color_index, is_typing, active_clause)
            presence_db[params[1]] = {
                "user_id": params[1],
                "username": params[2],
                "color_index": params[3],
                "is_typing": params[4],
                "active_clause": params[5],
            }
            return 1
        return 0

    from services import pg_service
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)
    monkeypatch.setattr(pg_service, "query", mock_query)
    monkeypatch.setattr(pg_service, "execute", mock_execute)

    req = EditorHeartbeatRequest(
        is_typing=True,
        active_clause="PASAL 3",
        known_updated_at="2026-11-11T11:59:00.000000Z",
    )
    user = {"id": "u-owner", "username": "boss"}
    res = editor_heartbeat("rep-1", req, current_user=user)

    assert len(res["active_users"]) == 1
    assert res["active_users"][0]["is_typing"] is True
    assert res["active_users"][0]["active_clause"] == "PASAL 3"
    assert res["latest_draft"] is not None
    assert res["latest_draft"]["version"] == 4


def test_versions_create_and_restore(monkeypatch):
    versions_db = []
    draft = {"content_html": "<p>Awal</p>", "version": 1}

    def mock_query_one(sql, params):
        if "FROM compliance_history" in sql:
            return {"id": params[0], "filename": "kontrak.pdf", "user_id": "u-owner", "notebook_id": "nb-1"}
        if "SELECT COALESCE(MAX(version_number), 0) + 1" in sql:
            return {"next_ver": len(versions_db) + 1}
        if "FROM document_editor_versions WHERE id = %s" in sql:
            for v in versions_db:
                if v["id"] == params[0]:
                    return v
            return None
        return None

    def mock_query(sql, params=None):
        if "FROM document_editor_versions" in sql:
            return versions_db
        return []

    def mock_execute(sql, params=None):
        if "INSERT INTO document_editor_versions" in sql:
            versions_db.append({
                "id": params[0],
                "report_id": params[1],
                "version_number": params[2],
                "title": params[3],
                "snapshot_html": params[4],
                "created_by": params[5],
                "author_name": params[6],
                "color_index": params[7],
                "changes_json": params[8],
                "created_at": "2026-11-11T12:00:00Z",
            })
            return 1
        if "document_editor_drafts" in sql:
            draft["content_html"] = params[3]
            draft["version"] += 1
            return 1
        return 0

    from services import pg_service
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)
    monkeypatch.setattr(pg_service, "query", mock_query)
    monkeypatch.setattr(pg_service, "execute", mock_execute)

    user = {"id": "u-owner", "username": "boss"}
    # 1. Create version v1
    v_req = CreateVersionRequest(
        content_html="<p>Versi 1 disetujui</p>",
        title="Finalisasi Klausul",
        changes=[{"id": "chg-1", "author": "boss", "type": "insertion", "text": "tambahan"}],
    )
    created = create_editor_version("rep-1", v_req, current_user=user)
    assert created["version_number"] == 1
    assert created["title"] == "Finalisasi Klausul"

    # 2. List versions
    listed = list_editor_versions("rep-1", current_user=user)
    assert len(listed["versions"]) == 1
    assert listed["versions"][0]["author_name"] == "boss"

    # 3. Restore version
    restored = restore_editor_version("rep-1", created["id"], current_user=user)
    assert restored["status"] == "restored"
    assert restored["content_html"] == "<p>Versi 1 disetujui</p>"
    assert draft["content_html"] == "<p>Versi 1 disetujui</p>"


