import pytest
from fastapi import HTTPException
from services import doc_notifications, pg_service
from routers.editor import (
    reply_editor_comment,
    resolve_editor_comment,
    mark_contract_read,
    CommentReplyRequest,
    CommentResolveRequest,
)
from routers.notification import get_unread_documents, toggle_notification_mute, MuteToggleRequest


@pytest.fixture(autouse=True)
def skip_table_ensure(monkeypatch):
    monkeypatch.setattr(doc_notifications, "ensure_notification_tables", lambda: None)
    monkeypatch.setattr("routers.editor._ensure_editor_tables", lambda: None)


def test_notify_broadcast_excludes_actor_and_muted_users(monkeypatch):
    inserted = []
    updated = []

    def mock_query_one(sql, params=None):
        if "FROM compliance_history" in sql:
            return {"id": "doc-1", "user_id": "owner-1", "notebook_id": "nb-1"}
        if "FROM document_notifications" in sql:
            # owner-1 already has an unread row within 15m; collab-2 does not
            if params[2] == "owner-1":
                return {"id": "dnotif-existing"}
            return None
        return None

    def mock_query(sql, params=None):
        if "FROM workspace_collaborators" in sql:
            return [{"user_id": "collab-1"}, {"user_id": "collab-2"}, {"user_id": "collab-muted"}]
        if "FROM notification_mutes" in sql:
            return [{"user_id": "collab-muted"}]
        return []

    def mock_execute(sql, params=None):
        if "INSERT INTO document_notifications" in sql:
            inserted.append(params)
        elif "UPDATE document_notifications" in sql:
            updated.append(params)
        return 1

    monkeypatch.setattr(pg_service, "query_one", mock_query_one)
    monkeypatch.setattr(pg_service, "query", mock_query)
    monkeypatch.setattr(pg_service, "execute", mock_execute)

    # collab-1 makes the edit -> should NOT notify collab-1 or collab-muted
    doc_notifications.notify_collaborators("doc-1", "collab-1", event_type="document_edited")

    updated_ids = [u[1] for u in updated]
    inserted_recipients = [i[1] for i in inserted]

    assert "dnotif-existing" in updated_ids
    assert inserted_recipients == ["collab-2"]
    assert "collab-1" not in inserted_recipients
    assert "collab-muted" not in inserted_recipients


def test_notify_targeted_only_notifies_specified_accessible_users(monkeypatch):
    inserted = []

    def mock_query_one(sql, params=None):
        if "FROM compliance_history" in sql:
            return {"id": "doc-1", "user_id": "owner-1", "notebook_id": "nb-1"}
        return None

    def mock_query(sql, params=None):
        if "FROM workspace_collaborators" in sql:
            return [{"user_id": "collab-1"}, {"user_id": "collab-2"}]
        if "FROM notification_mutes" in sql:
            return []
        return []

    def mock_execute(sql, params=None):
        if "INSERT INTO document_notifications" in sql:
            inserted.append(params)
        return 1

    monkeypatch.setattr(pg_service, "query_one", mock_query_one)
    monkeypatch.setattr(pg_service, "query", mock_query)
    monkeypatch.setattr(pg_service, "execute", mock_execute)

    # Targeted reply to owner-1 and a removed outsider; collab-2 must NOT be notified
    doc_notifications.notify_collaborators(
        "doc-1",
        "collab-1",
        event_type="comment_reply",
        target_user_ids=["owner-1", "removed-user", "collab-1"],
        reference_id="cmt-99",
    )

    inserted_recipients = [i[1] for i in inserted]
    assert inserted_recipients == ["owner-1"]
    assert inserted[0][4] == "comment_reply"
    assert inserted[0][5] == "cmt-99"


def test_reply_editor_comment_routes_to_thread_participants_only(monkeypatch):
    captured = {}

    def mock_verify(report_id, user_id):
        return {"id": report_id, "user_id": user_id, "owner_id": "owner-1", "notebook_id": "nb-1"}

    def mock_query_one(sql, params=None):
        if "FROM document_editor_comments" in sql:
            return {
                "user_id": "owner-1",
                "replies_json": '[{"id": "r1", "user_id": "collab-2", "author_name": "Budi", "text": "ok"}]',
            }
        return None

    def mock_execute_returning(sql, params=None):
        return [{
            "id": "cmt-1",
            "report_id": "doc-1",
            "author_name": "Owner",
            "selected_text": "Pasal 1",
            "comment_text": "Cek ini",
            "replies_json": params[0],
            "resolved": False,
            "created_at": "2026-11-16T03:00:00Z",
            "updated_at": "2026-11-16T03:05:00Z",
        }]

    def mock_notify(report_id, actor_id, event_type="document_edited", target_user_ids=None, reference_id=None):
        captured["report_id"] = report_id
        captured["actor_id"] = actor_id
        captured["event_type"] = event_type
        captured["target_user_ids"] = list(target_user_ids or [])
        captured["reference_id"] = reference_id

    monkeypatch.setattr("routers.editor.verify_report_access", mock_verify)
    monkeypatch.setattr(pg_service, "query_one", mock_query_one)
    monkeypatch.setattr(pg_service, "execute_returning", mock_execute_returning)
    monkeypatch.setattr(doc_notifications, "notify_collaborators", mock_notify)

    res = reply_editor_comment(
        "doc-1",
        "cmt-1",
        CommentReplyRequest(reply_text="Sudah diperbaiki"),
        current_user={"id": "collab-1", "username": "Sari"},
    )

    assert len(res["comment"]["replies"]) == 2
    assert captured["event_type"] == "comment_reply"
    assert captured["actor_id"] == "collab-1"
    assert set(captured["target_user_ids"]) == {"owner-1", "collab-2", "collab-1"}
    assert captured["reference_id"] == "cmt-1"


def test_resolve_editor_comment_notifies_comment_author_only(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "routers.editor.verify_report_access",
        lambda r, u: {"id": r, "user_id": u},
    )
    monkeypatch.setattr(
        pg_service,
        "execute_returning",
        lambda sql, params=None: [{
            "id": "cmt-1",
            "report_id": "doc-1",
            "user_id": "owner-1",
            "author_name": "Owner",
            "selected_text": "Pasal 1",
            "comment_text": "Cek",
            "replies_json": "[]",
            "resolved": True,
            "created_at": "2026-11-16T03:00:00Z",
            "updated_at": "2026-11-16T03:05:00Z",
        }],
    )
    monkeypatch.setattr(
        doc_notifications,
        "notify_collaborators",
        lambda report_id, actor_id, event_type="document_edited", target_user_ids=None, reference_id=None: captured.update({
            "event_type": event_type,
            "target_user_ids": list(target_user_ids or []),
            "reference_id": reference_id,
        }),
    )

    resolve_editor_comment(
        "doc-1",
        "cmt-1",
        CommentResolveRequest(resolved=True),
        current_user={"id": "collab-1", "username": "Sari"},
    )

    assert captured == {
        "event_type": "comment_resolved",
        "target_user_ids": ["owner-1"],
        "reference_id": "cmt-1",
    }


def test_get_unread_summary_aggregates_documents_and_notebooks(monkeypatch):
    def mock_query(sql, params=None):
        if "FROM document_notifications" in sql:
            return [
                {
                    "report_id": "doc-1",
                    "actor_id": "u2",
                    "event_type": "document_edited",
                    "reference_id": None,
                    "actor_name": "Budi",
                    "notebook_id": "nb-1",
                    "updated_at_iso": "2026-11-16T03:00:00Z",
                },
                {
                    "report_id": "doc-1",
                    "actor_id": "u3",
                    "event_type": "comment_reply",
                    "reference_id": "cmt-1",
                    "actor_name": "Sari",
                    "notebook_id": "nb-1",
                    "updated_at_iso": "2026-11-16T03:05:00Z",
                },
                {
                    "report_id": "doc-2",
                    "actor_id": "u2",
                    "event_type": "document_edited",
                    "reference_id": None,
                    "actor_name": "Budi",
                    "notebook_id": "nb-1",
                    "updated_at_iso": "2026-11-16T03:02:00Z",
                },
            ]
        if "FROM notification_mutes" in sql:
            return [{"target_type": "document", "target_id": "doc-99"}]
        return []

    monkeypatch.setattr(pg_service, "query", mock_query)

    summary = get_unread_documents(current_user={"id": "u1"})
    assert summary["unread_by_document"]["doc-1"]["event_type"] == "comment_reply"
    assert summary["unread_by_document"]["doc-1"]["latest_editor_name"] == "Sari"
    assert summary["unread_by_document"]["doc-1"]["other_editors_count"] == 1
    assert summary["unread_by_notebook"]["nb-1"]["unread_doc_count"] == 2
    assert summary["unread_by_notebook"]["nb-1"]["latest_editor_name"] == "Sari"
    assert summary["mutes"] == {"notebooks": [], "documents": ["doc-99"]}


def test_mark_read_and_mute_endpoints(monkeypatch):
    calls = []
    monkeypatch.setattr("routers.editor.verify_report_access", lambda r, u: {"id": r})
    monkeypatch.setattr(pg_service, "execute", lambda sql, params=None: calls.append((sql, params)) or 1)

    mark_contract_read("doc-1", current_user={"id": "u1"})
    assert any("document_user_states" in sql for sql, _ in calls)
    assert any("UPDATE document_notifications" in sql for sql, _ in calls)

    res = toggle_notification_mute(
        MuteToggleRequest(target_type="notebook", target_id="nb-1", muted=True),
        current_user={"id": "u1"},
    )
    assert res["muted"] is True
    assert any("INSERT INTO notification_mutes" in sql for sql, _ in calls)
