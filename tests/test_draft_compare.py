"""Unit tests for draft-to-draft clause matching (pure, no DB)."""
from services import draft_compare as dc


def _clause(pasal, posisi="NETRAL", status="SESUAI", body="Isi klausul.", catatan=""):
    return {
        "pasal": pasal,
        "isi_pasal": body,
        "status": status,
        "posisi_perusahaan": posisi,
        "catatan_posisi": catatan,
        "kelebihan": [{"poin": f"plus {pasal}", "dasar": "komersial"}] if posisi == "MENGUNTUNGKAN" else [],
        "kekurangan": [{"poin": f"minus {pasal}", "dasar": "komersial"}] if posisi == "MERUGIKAN" else [],
    }


def _draft(rid, clauses, filename=None, skor_posisi=0):
    return {
        "report_id": rid,
        "filename": filename or f"{rid}.docx",
        "created_at": f"2026-11-0{rid[-1]} 10:00:00",
        "summary": {"skor_kepatuhan": 80, "skor_posisi": skor_posisi,
                    "pihak_kita": "PT TCI", "peran_kita": "Penyedia",
                    "klausul_menguntungkan": 1, "klausul_merugikan": 1, "klausul_netral": 1},
        "clauses": clauses,
    }


def test_improved_clause_reports_improved():
    out = dc.build_comparison([
        _draft("d1", [_clause("Pasal 1", posisi="MERUGIKAN", body="Denda tanpa batas.")]),
        _draft("d2", [_clause("Pasal 1", posisi="MENGUNTUNGKAN", body="Denda dibatasi nilai kontrak.")]),
    ])
    row = out["clauses"][0]
    assert row["change"] == "improved"
    assert out["summary"]["improved"] == 1
    assert row["cells"][0]["posisi_perusahaan"] == "MERUGIKAN"
    assert row["cells"][1]["posisi_perusahaan"] == "MENGUNTUNGKAN"


def test_worsened_clause_reports_worsened():
    out = dc.build_comparison([
        _draft("d1", [_clause("Pasal 2", posisi="MENGUNTUNGKAN")]),
        _draft("d2", [_clause("Pasal 2", posisi="MERUGIKAN")]),
    ])
    assert out["clauses"][0]["change"] == "worsened"


def test_added_and_removed_clauses():
    old = "Pemberi kerja menyediakan perangkat kerja dan menanggung biaya perjalanan dinas."
    new = "Penerima kerja dilarang mengalihkan sebagian pekerjaan kepada pihak ketiga tanpa izin."
    out = dc.build_comparison([
        _draft("d1", [_clause("Pasal 1"), _clause("Pasal 2", body=old)]),
        _draft("d2", [_clause("Pasal 1"), _clause("Pasal 9", body=new)]),
    ])
    by_key = {r["key"]: r["change"] for r in out["clauses"]}
    assert by_key["pasal 9#0"] == "added"
    assert by_key["pasal 2#0"] == "removed"
    assert out["summary"]["added"] == 1
    assert out["summary"]["removed"] == 1


def test_renumbered_clause_matches_by_wording():
    body = "Pembayaran dilakukan paling lambat tiga puluh hari setelah tagihan diterima."
    out = dc.build_comparison([
        _draft("d1", [_clause("Pasal 5", body=body)]),
        _draft("d2", [_clause("Pasal 6", body=body)]),
    ])
    assert len(out["clauses"]) == 1
    row = out["clauses"][0]
    assert all(c["present"] for c in row["cells"])
    assert row["change"] == "unchanged"
    assert out["summary"]["added"] == 0 and out["summary"]["removed"] == 0


def test_duplicate_labels_disambiguated_by_occurrence():
    out = dc.build_comparison([
        _draft("d1", [_clause("Pasal 7", body="Ketentuan pembayaran."),
                      _clause("Pasal 7", body="Ketentuan kerahasiaan.")]),
        _draft("d2", [_clause("Pasal 7", body="Ketentuan pembayaran."),
                      _clause("Pasal 7", body="Ketentuan kerahasiaan.")]),
    ])
    assert len(out["clauses"]) == 2
    assert {r["key"] for r in out["clauses"]} == {"pasal 7#0", "pasal 7#1"}


def test_status_change_reported_without_blending_axes():
    out = dc.build_comparison([
        _draft("d1", [_clause("Pasal 3", status="BERESIKO", posisi="MERUGIKAN")]),
        _draft("d2", [_clause("Pasal 3", status="SESUAI", posisi="MERUGIKAN")]),
    ])
    row = out["clauses"][0]
    assert row["status_change"] == "BERESIKO → SESUAI"
    assert row["change"] == "unchanged"  # position did not move


def test_draft_metadata_passes_through():
    out = dc.build_comparison([_draft("d1", [_clause("Pasal 1")], skor_posisi=40)])
    assert out["drafts"][0]["skor_posisi"] == 40
    assert out["drafts"][0]["pihak_kita"] == "PT TCI"
    assert out["drafts"][0]["peran_kita"] == "Penyedia"


def test_empty_comparison_is_safe():
    out = dc.build_comparison([])
    assert out["drafts"] == []
    assert out["clauses"] == []
    assert out["summary"]["improved"] == 0


def test_missing_field_defaults_to_neutral():
    out = dc.build_comparison([
        _draft("d1", [{"pasal": "Pasal 1", "isi_pasal": "x", "status": "SESUAI"}]),
        _draft("d2", [{"pasal": "Pasal 1", "isi_pasal": "x", "status": "SESUAI"}]),
    ])
    assert out["clauses"][0]["cells"][0]["posisi_perusahaan"] == "NETRAL"
