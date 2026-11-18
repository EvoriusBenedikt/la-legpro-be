"""Tests for Legal Audit Brief PDF generation via PyMuPDF Story."""
import os
import sys

import fitz

API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "api"))
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from services.pdf_report import generate_analysis_pdf  # noqa: E402


def test_generate_analysis_pdf_includes_all_sections_and_strips_seg_tags():
    pdf_bytes = generate_analysis_pdf(
        filename="30092026 NDA Lintasarta dan Helios.docx",
        summary={
            "jenis_dokumen": "Perjanjian Kerahasiaan",
            "sektor_bisnis": "Teknologi",
            "pihak_pertama": "PT Helios Informatika Nusantara",
            "pihak_kedua": "PT Aplikanusa Lintasarta",
            "pokok_perjanjian": "Menjaga kerahasiaan informasi proyek kerja sama.",
            "skor_kepatuhan": 33,
            "skor_posisi": -33,
        },
        results=[
            {
                "pasal": "Pasal 2",
                "isi_pasal": "[seg_028] Kewajiban Menjaga Informasi Rahasia tanpa pengecualian.",
                "status": "BERESIKO",
                "posisi_perusahaan": "MERUGIKAN",
                "pasal_regulasi_terkait": "Pasal 2 ayat (1) Perjanjian Kerahasiaan",
                "penjelasan": "Klausul ini membatasi pengungkapan tanpa pengecualian direksi.",
                "rekomendasi": "Tambahkan pengecualian untuk direksi, karyawan, dan afiliasi.",
                "kelebihan": [{"poin": "Melindungi data rahasia"}],
                "kekurangan": [{"poin": "Terlalu membatasi operasional internal"}],
                "supporting_regulations": [
                    {
                        "jenis": "UU",
                        "nomor": "27 Tahun 2022",
                        "sektor": "PDP",
                        "teks": "Pemrosesan data pribadi wajib memenuhi dasar pemrosesan.",
                    }
                ],
            },
            {
                "pasal": "Pasal 1",
                "isi_pasal": "[seg_013] Dalam Perjanjian ini istilah berikut memiliki arti...",
                "status": "TIDAK DIATUR",
                "posisi_perusahaan": "NETRAL",
                "penjelasan": "Klausul definisi standar.",
            },
        ],
        missing_clauses=[
            {
                "title": "Force Majeure",
                "severity": "CRITICAL",
                "reason": "Melindungi para pihak dari keadaan kahar.",
                "suggested_clause": "Tidak ada pihak yang bertanggung jawab atas keterlambatan akibat Keadaan Kahar.",
            }
        ],
        locale="en",
    )

    assert pdf_bytes.startswith(b"%PDF-")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    full_text = "\n".join(page.get_text("text") for page in doc)
    doc.close()

    assert "30092026 NDA Lintasarta dan Helios.docx" in full_text
    assert "33%" in full_text
    assert "-33" in full_text
    assert "[seg_028]" not in full_text
    assert "[seg_013]" not in full_text
    assert "RECOMMENDED REDLINE" in full_text
    assert "Force Majeure" in full_text
    assert "Complete Clause Log" in full_text
    assert "Page 1 of" in full_text


def test_export_pdf_endpoint_returns_valid_pdf():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    os.environ.setdefault("JWT_SECRET", "test-secret-for-unit-tests")
    import auth
    import history

    app = FastAPI()
    app.include_router(history.router, prefix="/api")
    app.dependency_overrides[auth.get_current_user] = lambda: {"id": "u-1", "role": "pengguna"}

    client = TestClient(app)
    res = client.post(
        "/api/compliance-history/export-pdf",
        json={
            "filename": "30092026 NDA Lintasarta dan Helios.docx",
            "summary": {"skor_kepatuhan": 33, "jenis_dokumen": "NDA"},
            "results": [
                {
                    "pasal": "Pasal 2",
                    "isi_pasal": "Kewajiban kerahasiaan",
                    "status": "BERESIKO",
                    "penjelasan": "Perlu pengecualian.",
                    "rekomendasi": "Tambahkan pengecualian direksi.",
                }
            ],
            "locale": "en",
        },
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert "attachment;" in res.headers.get("content-disposition", "")
    assert res.content.startswith(b"%PDF-")
