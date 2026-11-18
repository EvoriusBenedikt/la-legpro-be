"""Unit tests for workspace-scoped reference documents in chatbot retrieval and prompting."""
from unittest.mock import MagicMock

import pytest


def test_retrieve_workspace_references_filters_excluded(monkeypatch):
    from services import pg_service, rag_service

    monkeypatch.setattr(
        pg_service,
        "query",
        lambda sql, params=None: [
            {
                "id": "internal_keep1234_0",
                "doc_id": None,
                "text": "SOP Vendor Pasal 1",
                "window_context": "SOP Vendor Pasal 1",
                "jenis": "Dokumen Internal",
                "nomor": "sop.pdf",
                "sektor": "Internal",
                "judul": "sop.pdf",
            },
            {
                "id": "internal_drop5678_0",
                "doc_id": None,
                "text": "Excluded doc",
                "window_context": "Excluded doc",
                "jenis": "Dokumen Internal",
                "nomor": "drop.pdf",
                "sektor": "Internal",
                "judul": "drop.pdf",
            },
        ],
    )

    assert rag_service._retrieve_workspace_references("[0.1]", "u1", [], set()) == []

    refs = rag_service._retrieve_workspace_references(
        "[0.1]", "u1", ["ws-1"], {"internal:drop5678"}
    )
    assert len(refs) == 1
    assert refs[0]["id"] == "internal_keep1234_0"
    assert refs[0]["is_workspace_ref"] is True


def test_resolve_workspace_report_ids_includes_notebook(monkeypatch):
    from services import pg_service, rag_service

    monkeypatch.setattr(
        pg_service,
        "query_one",
        lambda sql, params=None: {"notebook_id": "nb-1"} if params == ("doc-2",) else None,
    )
    assert rag_service._resolve_workspace_report_ids("doc-2") == ["doc-2", "nb-1"]
    assert rag_service._resolve_workspace_report_ids(None) == []


def test_retrieve_contexts_cag_keeps_workspace_refs(monkeypatch):
    from services import pg_service, rag_service

    monkeypatch.setattr(rag_service, "embed_query", lambda q: "[0.1]")
    monkeypatch.setattr(rag_service, "get_user_exclusions", lambda uid: set())

    ws_chunk = {
        "id": "internal_ws123456_0",
        "doc_id": None,
        "text": "Workspace SOP clause",
        "window_context": "Workspace SOP clause",
        "jenis": "Dokumen Internal",
        "nomor": "ws-ref.pdf",
        "sektor": "Internal",
        "judul": "ws-ref.pdf",
    }

    def fake_query(sql, params=None):
        if "FROM access_grants" in sql:
            return []
        if "FROM regulations" in sql and "klasifikasi" in sql:
            return [{"id": 10, "klasifikasi": "Umum"}]
        if "visibility = 'private'" in sql and "report_id = ANY(%s)" in sql:
            return [ws_chunk]
        if "SELECT text FROM chunks WHERE doc_id = %s" in sql:
            return [{"text": "Public UU ITE text"}]
        if "local_path FROM regulations" in sql:
            return [{"doc_id": "10", "local_path": "uu_ite.pdf"}]
        return []

    monkeypatch.setattr(pg_service, "query", fake_query)
    monkeypatch.setattr(pg_service, "query_one", lambda sql, params=None: None)

    class _FakeCursor:
        def fetchall(self):
            return [
                {
                    "id": "pub_1",
                    "doc_id": "10",
                    "jenis": "UU",
                    "nomor": "11 Tahun 2008",
                    "sektor": "Teknologi",
                    "judul": "UU ITE",
                }
            ]

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            return _FakeCursor()

    monkeypatch.setattr(pg_service, "get_conn", lambda: _FakeConn())

    ctxs = rag_service.retrieve_contexts(
        "Periksa klausul kontrak",
        {"id": "u1", "role": "pengguna"},
        hybrid_cag=True,
        report_id="ws-1",
    )
    ids = [c["id"] for c in ctxs]
    assert "internal_ws123456_0" in ids
    assert "10" in ids


def test_chat_prompt_highlights_workspace_reference(monkeypatch):
    import asyncio
    import psycopg_pool
    from services import db_service, kg_service, pg_service, rag_service

    monkeypatch.setattr(pg_service, "ConnectionPool", lambda *a, **k: None)
    monkeypatch.setattr(pg_service, "get_pool", lambda: None)
    monkeypatch.setattr(pg_service, "get_conn", lambda: None)
    monkeypatch.setattr(
        pg_service,
        "query_one",
        lambda sql, params=None: (
            {"n": 1}
            if "COUNT(*)" in sql
            else (
                {
                    "id": "ws-1",
                    "filename": "kontrak-vendor.pdf",
                    "company_name": "PT Mitra",
                    "expiration_date": "2027-01-01",
                    "results_json": '{"summary": {"skor_kepatuhan": 80}, "results": []}',
                }
                if "FROM compliance_history" in sql
                else None
            )
        ),
    )
    monkeypatch.setattr(pg_service, "query", lambda *a, **k: [])

    from routers import chat

    monkeypatch.setattr(
        rag_service,
        "retrieve_contexts",
        lambda q, current_user=None, report_id=None: [
            {
                "id": "internal_ws123456_0",
                "doc_id": None,
                "jenis": "Dokumen Internal",
                "nomor": "sop-vendor.pdf",
                "sektor": "Internal",
                "judul": "sop-vendor.pdf",
                "text": "Kewajiban SLA minimal 99.9%",
                "rerank_score": None,
                "filename": None,
                "is_workspace_ref": True,
            }
        ],
    )
    monkeypatch.setattr(kg_service, "retrieve_graph_contexts", lambda *a, **k: "")
    monkeypatch.setattr(db_service, "log_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        pg_service,
        "query_one",
        lambda sql, params=None: (
            {"n": 1}
            if "COUNT(*)" in sql
            else (
                {
                    "id": "ws-1",
                    "filename": "kontrak-vendor.pdf",
                    "company_name": "PT Mitra",
                    "expiration_date": "2027-01-01",
                    "results_json": '{"summary": {"skor_kepatuhan": 80}, "results": []}',
                }
                if "FROM compliance_history" in sql
                else None
            )
        ),
    )
    monkeypatch.setattr(pg_service, "query", lambda *a, **k: [])

    captured_calls = []

    def fake_call_llm(messages, **kwargs):
        captured_calls.append(messages)
        if len(captured_calls) == 1:
            return "Ya, klausul SLA perlu disesuaikan dengan referensi workspace [1]."
        return "Revisi SLA Kontrak"

    monkeypatch.setattr(chat, "call_llm", fake_call_llm)

    req = chat.ChatRequest(
        report_id="ws-1",
        messages=[chat.ChatMessage(role="user", content="Bandingkan kontrak ini dengan dokumen referensi")],
    )
    res = asyncio.run(chat.chat_endpoint(req, current_user={"id": "u1", "role": "pengguna"}))
    assert "referensi workspace" in res.answer
    assert len(captured_calls) >= 1
    sys_msg = captured_calls[0][0]["content"]
    usr_msg = captured_calls[0][1]["content"]
    assert "DOKUMEN REFERENSI WORKSPACE" in sys_msg
    assert "SUMBER [1] (DOKUMEN REFERENSI WORKSPACE)" in usr_msg
    assert "kontrak-vendor.pdf" in sys_msg
