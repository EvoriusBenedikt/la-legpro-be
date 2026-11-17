import docx
import fitz
import pytest

from services import bilingual_extractor
from routers.editor import _pdf_table_html


def test_clean_bilingual_cell_dehyphenates_and_preserves_spaced_hyphens():
    raw = "The Parties have agreed to the terms and condi-\ntions (seba-\ngaimana) on hal - hal berikut."
    cleaned = bilingual_extractor.clean_bilingual_cell(raw)
    assert "conditions" in cleaned
    assert "sebagaimana" in cleaned
    assert "hal - hal" in cleaned


def test_detect_bilingual_columns_en_id_and_id_en():
    rows_en_id = [
        [
            "Therefore, based on the matters described above, the Parties have agreed to the following terms and conditions of this Agreement.",
            "Maka, berdasarkan hal - hal yang telah diuraikan di atas, Para Pihak telah menyetujui syarat dan ketentuan Perjanjian ini.",
        ]
    ]
    is_bi, order = bilingual_extractor.detect_bilingual_columns(rows_en_id)
    assert is_bi is True
    assert order == ["en", "id"]

    rows_id_en = [[r[1], r[0]] for r in rows_en_id]
    is_bi2, order2 = bilingual_extractor.detect_bilingual_columns(rows_id_en)
    assert is_bi2 is True
    assert order2 == ["id", "en"]


def test_detect_governing_language_defaults_to_id_and_detects_en():
    segs_default = [{"en": "The Parties agree to cooperate.", "id": "Para Pihak sepakat untuk bekerja sama."}]
    assert bilingual_extractor.detect_governing_language(segs_default) == "id"

    segs_en = [{
        "en": "In the event of any discrepancy, the English version shall prevail.",
        "id": "Apabila terdapat perbedaan, maka versi bahasa Inggris yang berlaku.",
    }]
    assert bilingual_extractor.detect_governing_language(segs_en) == "en"


def test_extract_bilingual_from_docx_preserves_interleaved_order(tmp_path):
    docx_path = tmp_path / "bilingual.docx"
    doc = docx.Document()
    doc.add_paragraph("COOPERATION AGREEMENT / PERJANJIAN KERJASAMA")
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "ARTICLE 1 — DEFINITIONS"
    tbl.cell(0, 1).text = "PASAL 1 — DEFINISI"
    tbl.cell(1, 0).text = "The Parties have agreed to the terms and conditions of this Agreement."
    tbl.cell(1, 1).text = "Para Pihak telah menyetujui syarat dan ketentuan dalam Perjanjian ini."
    doc.add_paragraph("IN WITNESS WHEREOF / DEMIKIAN PERJANJIAN INI")
    doc.save(str(docx_path))

    res = bilingual_extractor.extract_bilingual_from_docx(str(docx_path))
    assert res["is_bilingual"] is True
    assert res["column_order"] == ["en", "id"]
    assert res["layout_mode"] == "bilingual_parallel"
    types = [s["type"] for s in res["segments"]]
    assert types == ["standalone_block", "section_header", "paired_clause", "standalone_block"]


def test_extract_bilingual_from_pdf_bordered_table_and_html(tmp_path):
    pdf_path = tmp_path / "bilingual.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # Draw a 2-col bordered table
    x0, x1, x2 = 40, 297, 555
    y0, y1 = 80, 220
    page.draw_rect(fitz.Rect(x0, y0, x2, y1))
    page.draw_line(fitz.Point(x1, y0), fitz.Point(x1, y1))
    page.insert_textbox(
        fitz.Rect(x0 + 6, y0 + 6, x1 - 6, y1 - 6),
        "Therefore, the Parties have agreed to the terms and condi-\ntions of this Agreement.",
        fontsize=10,
    )
    page.insert_textbox(
        fitz.Rect(x1 + 6, y0 + 6, x2 - 6, y1 - 6),
        "Maka, Para Pihak telah menyetujui syarat dan ketentuan (seba-\ngaimana) Perjanjian ini.",
        fontsize=10,
    )
    doc.save(str(pdf_path))
    doc.close()

    res = bilingual_extractor.extract_bilingual_from_pdf(str(pdf_path))
    assert res["is_bilingual"] is True
    assert res["column_order"] == ["en", "id"]
    paired = [s for s in res["segments"] if s["type"] == "paired_clause"]
    assert len(paired) >= 1
    assert "conditions" in paired[0]["en"]
    assert "sebagaimana" in paired[0]["id"]

    table_html = _pdf_table_html([[paired[0]["en"], paired[0]["id"]]])
    assert "bilingual-contract-table" in table_html
    assert 'class="col-en" lang="en"' in table_html
    assert 'class="col-id" lang="id"' in table_html


def test_stitch_split_segments_across_pages():
    segs = [
        {"segment_id": "seg_001", "type": "paired_clause", "en": "The Vendor shall deliver", "id": "Vendor wajib menyerahkan", "page": 1},
        {"segment_id": "seg_002", "type": "paired_clause", "en": "the services on time.", "id": "layanan tepat waktu.", "page": 2},
    ]
    stitched = bilingual_extractor._stitch_split_segments(segs)
    assert len(stitched) == 1
    assert stitched[0]["en"] == "The Vendor shall deliver the services on time."
    assert stitched[0]["id"] == "Vendor wajib menyerahkan layanan tepat waktu."


def test_find_translation_discrepancies_and_governing_text():
    segs = [
        {
            "segment_id": "seg_001",
            "type": "paired_clause",
            "en": "Vendor shall indemnify the Client up to 100% of fees.",
            "id": "Vendor dapat mengganti kerugian Klien hingga 50% dari biaya.",
        }
    ]
    discrepancies = bilingual_extractor.find_translation_discrepancies(segs)
    categories = {d["category"] for d in discrepancies}
    assert "obligation_mismatch" in categories
    assert "numeric_mismatch" in categories

    gov_text = bilingual_extractor.build_governing_text(segs, "id")
    assert gov_text == "[seg_001] Vendor dapat mengganti kerugian Klien hingga 50% dari biaya."
