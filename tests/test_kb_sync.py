"""Tests for kb_sync: HTML cleaning, staleness tracking, and real compliance recheck."""
import json
import os
import sys

os.environ.setdefault("JWT_SECRET", "test-secret-for-unit-tests")
API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "api"))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from services import kb_sync  # noqa: E402


def test_html_to_plain_drops_del_and_keeps_ins():
    html = (
        '<p>Pasal 1: <del class="ed-suggest-del">Old risky text</del>'
        '<ins class="ed-suggest-ins">New compliant text</ins>.</p>'
        '<p>Pasal 2: Unchanged clause.</p>'
    )
    plain = kb_sync.html_to_plain_for_recheck(html)
    assert "Old risky text" not in plain
    assert "New compliant text" in plain
    assert "Pasal 2: Unchanged clause." in plain


def test_recheck_report_compliance_updates_score_and_clears_stale(monkeypatch):
    stored_report = {
        "id": "rep-1",
        "kb_stale": True,
        "results_json": {
            "summary": {
                "jenis_dokumen": "NDA",
                "pihak_pertama": "PT A",
                "pihak_kedua": "PT B",
                "sektor_bisnis": "umum",
                "pokok_perjanjian": "Kerahasiaan",
                "skor_kepatuhan": 50,
            },
            "results": [
                {"pasal": "Pasal 1", "isi_pasal": "Old risky text", "status": "BERESIKO"},
                {"pasal": "Pasal 2", "isi_pasal": "Safe clause", "status": "SESUAI"},
            ],
        },
    }
    executed = []

    monkeypatch.setattr(kb_sync, "ensure_kb_sync_columns", lambda: None)
    monkeypatch.setattr(kb_sync.pg_service, "query_one", lambda q, params=(): stored_report if "compliance_history" in q else None)
    monkeypatch.setattr(kb_sync.pg_service, "execute", lambda q, params=(): executed.append((q, params)))

    import services.rag_service as rag_svc
    import routers.compliance as comp_router

    monkeypatch.setattr(rag_svc, "get_user_exclusions", lambda uid: set())
    monkeypatch.setattr(
        rag_svc,
        "extract_pasal_items",
        lambda plain: [
            {"pasal": "Pasal 1", "isi": "New compliant text"},
            {"pasal": "Pasal 2", "isi": "Safe clause"},
        ],
    )
    monkeypatch.setattr(
        comp_router,
        "process_single_pasal",
        lambda clause, *args, **kwargs: {
            "pasal": clause["pasal"],
            "isi_pasal": clause["isi"],
            "status": "SESUAI",
        },
    )

    res = kb_sync.recheck_report_compliance(
        "rep-1",
        '<p>Pasal 1: <del>Old risky text</del><ins>New compliant text</ins></p><p>Pasal 2: Safe clause</p>',
        "user-1",
    )

    assert res["previous_score"] == 50
    assert res["updated_score"] == 100
    assert res["resolved_risks"] == 1
    assert res["total_risks"] == 0
    assert res["kb_stale"] is False
    assert len(res["pasalResults"]) == 2
    assert any("kb_stale = FALSE" in q for q, _ in executed)
    persisted = json.loads(executed[-1][1][0])
    assert persisted["summary"]["skor_kepatuhan"] == 100
