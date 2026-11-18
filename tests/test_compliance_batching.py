"""Unit tests for compliance pipeline optimizations:
1. CrossEncoder reranking replacing per-clause LLM filtering
2. Batched clause verification with per-clause fallback
"""
import json
from unittest.mock import patch

from routers import compliance


class _FakeReranker:
    def __init__(self, scores):
        self._scores = scores
        self.calls = []

    def predict(self, pairs):
        self.calls.append(pairs)
        return self._scores


def _make_cand(idx: int, dist: float, text: str = "Regulasi teks"):
    return {
        "row": {"dist": dist, "id": f"c{idx}"},
        "text": f"{text} {idx}",
        "reg_name": f"UU Nomor {idx}",
    }


def _make_ctx(idx: int):
    return {
        "pasal_label": f"Pasal {idx}",
        "desc": f"Isi klausul kontrak nomor {idx} mengenai kewajiban para pihak.",
        "supporting_regulations": [{"regulation_id": idx, "jenis": "UU", "nomor": str(idx)}],
        "relevant_refs": [f"--- UU Nomor {idx} (berlaku) ---\nKetentuan regulasi {idx}"],
    }


def test_rerank_candidate_regulations_sorts_and_filters_by_score():
    cands = [_make_cand(0, 0.35), _make_cand(1, 0.40), _make_cand(2, 0.31)]
    fake = _FakeReranker([-0.5, -5.0, 2.3])

    with patch("services.rag_service.get_reranker", return_value=fake):
        kept = compliance._rerank_candidate_regulations("Klausul pembayaran", cands, min_score=-2.0)

    assert [c["reg_name"] for c in kept] == ["UU Nomor 2", "UU Nomor 0"]
    assert len(fake.calls) == 1
    assert len(fake.calls[0]) == 3


def test_rerank_candidate_regulations_falls_back_on_error():
    cands = [_make_cand(0, 0.42), _make_cand(1, 0.29), _make_cand(2, 0.35), _make_cand(3, 0.31)]

    with patch("services.rag_service.get_reranker", side_effect=RuntimeError("no model")):
        kept = compliance._rerank_candidate_regulations("Klausul pembayaran", cands)

    assert [c["reg_name"] for c in kept] == ["UU Nomor 1", "UU Nomor 3", "UU Nomor 2"]


def test_verify_clauses_batch_parses_results_in_order():
    ctxs = [_make_ctx(1), _make_ctx(2), _make_ctx(3)]
    payload = {
        "results": [
            {
                "id": 2,
                "status": "FATAL",
                "pasal_regulasi_terkait": "Pasal 3 UU 3",
                "penjelasan": "Melanggar ketentuan 3.",
                "rekomendasi": "Ubah klausul 3.",
                "ai_analysis": "Risiko tinggi.",
                "posisi_perusahaan": "MERUGIKAN",
                "catatan_posisi": "Berat sebelah.",
            },
            {
                "id": 0,
                "status": "SESUAI",
                "pasal_regulasi_terkait": "Pasal 1 UU 1",
                "penjelasan": "Patuh.",
                "rekomendasi": "",
                "ai_analysis": "",
                "posisi_perusahaan": "NETRAL",
                "catatan_posisi": "Seimbang.",
            },
            {
                "id": 1,
                "status": "BERESIKO",
                "pasal_regulasi_terkait": "Pasal 2 UU 2",
                "penjelasan": "Perlu klarifikasi.",
                "rekomendasi": "Perjelas.",
                "ai_analysis": "Menengah.",
                "posisi_perusahaan": "MENGUNTUNGKAN",
                "catatan_posisi": "Melindungi kita.",
            },
        ]
    }

    with patch.object(compliance, "call_llm", return_value=json.dumps(payload)) as mock_llm:
        results = compliance.verify_clauses_batch(
            ctxs, "PKS", "PT A", "PT B", "Keuangan", "Kerja Sama", "PT A", "Penyedia"
        )

    assert mock_llm.call_count == 1
    assert [r["pasal"] for r in results] == ["Pasal 1", "Pasal 2", "Pasal 3"]
    assert [r["status"] for r in results] == ["SESUAI", "BERESIKO", "FATAL"]
    assert [r["posisi_perusahaan"] for r in results] == ["NETRAL", "MENGUNTUNGKAN", "MERUGIKAN"]


def test_verify_clauses_batch_falls_back_for_missing_items():
    ctxs = [_make_ctx(1), _make_ctx(2)]
    partial_payload = {
        "results": [
            {
                "id": 0,
                "status": "SESUAI",
                "pasal_regulasi_terkait": "Pasal 1 UU 1",
                "penjelasan": "Patuh.",
                "rekomendasi": "",
                "ai_analysis": "",
                "posisi_perusahaan": "NETRAL",
                "catatan_posisi": "Seimbang.",
            }
        ]
    }
    single_payload = {
        "status": "BERESIKO",
        "pasal_regulasi_terkait": "Pasal 2 UU 2",
        "penjelasan": "Fallback single.",
        "rekomendasi": "Perbaiki.",
        "ai_analysis": "Risiko.",
        "posisi_perusahaan": "MERUGIKAN",
        "catatan_posisi": "Kurang baik.",
    }

    with patch.object(
        compliance,
        "call_llm",
        side_effect=[json.dumps(partial_payload), json.dumps(single_payload)],
    ) as mock_llm:
        results = compliance.verify_clauses_batch(
            ctxs, "PKS", "PT A", "PT B", "Keuangan", "Kerja Sama"
        )

    assert mock_llm.call_count == 2
    assert [r["pasal"] for r in results] == ["Pasal 1", "Pasal 2"]
    assert [r["status"] for r in results] == ["SESUAI", "BERESIKO"]
    assert results[1]["penjelasan"] == "Fallback single."
