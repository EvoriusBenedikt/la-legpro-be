import os
import fitz
import html
import re
import json

def extract_pdf_test(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    output_parts = []
    for page_idx, page in enumerate(doc):
        page_dict = page.get_text("dict")
        page_width = page.rect.width
        blocks = page_dict.get("blocks", [])
        text_blocks = [b for b in blocks if "lines" in b]
        text_blocks.sort(key=lambda b: (round(b["bbox"][1] / 6) * 6, b["bbox"][0]))

        page_parts = []
        for b in text_blocks:
            lines = b.get("lines", [])
            if not lines:
                continue
            line_htmls = []
            for l in lines:
                span_strs = []
                for s in l.get("spans", []):
                    st = s.get("text", "")
                    if st:
                        span_strs.append(html.escape(st))
                line_htmls.append("".join(span_strs))
            full_block_text = " ".join(line_htmls).strip().replace("\u200b", "")
            if full_block_text:
                page_parts.append(f'<p class="ed-doc-p">{full_block_text}</p>')

        if page_parts:
            if output_parts:
                output_parts.append('<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true"></div>')
            output_parts.extend(page_parts)

    full_html = "\n".join(output_parts)
    plain_text = re.sub(r"<[^>]+>", "", full_html).replace("\u200b", "").strip()
    if len(plain_text) < 40:
        return ""
    return full_html


def reconstruct_from_results_json(results_data: dict, filename: str = "Dokumen.pdf") -> str:
    PAGE_BREAK_HTML = '<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true"></div>'
    TARGET_PAGE_CHARS = 2100
    MIN_PAGE_CHARS = 1200

    clauses = []
    summary = {}
    if isinstance(results_data, dict):
        clauses = results_data.get("results") or results_data.get("report") or []
        summary = results_data.get("summary") or {}
    elif isinstance(results_data, list):
        clauses = results_data

    if not clauses:
        return ""

    doc_title = summary.get("jenis_dokumen") or os.path.splitext(filename)[0] or "Dokumen Kontrak"
    pokok = summary.get("pokok_perjanjian") or ""
    p1 = summary.get("pihak_pertama")
    p2 = summary.get("pihak_kedua")

    output_parts = [f'<h1 class="ed-paper-title">{html.escape(doc_title)}</h1>']
    if pokok:
        output_parts.append(f'<p class="ed-clause-subheading" style="text-align: center;"><strong>{html.escape(pokok)}</strong></p>')
    if p1 or p2:
        output_parts.append(f'<p class="ed-doc-p" style="text-align: center; margin-bottom: 24px;">Antara <strong>{html.escape(p1 or "-")}</strong> dan <strong>{html.escape(p2 or "-")}</strong></p>')

    current_page_chars = sum(len(re.sub(r'<[^>]+>', '', p)) for p in output_parts)

    for clause in clauses:
        pasal = (clause.get("pasal") or clause.get("judul") or "").strip()
        isi = (clause.get("isi_pasal") or clause.get("deskripsi") or clause.get("teks") or "").strip()
        clause_chars = len(pasal) + len(isi)

        if (current_page_chars + clause_chars > TARGET_PAGE_CHARS) and (current_page_chars >= MIN_PAGE_CHARS):
            output_parts.append(PAGE_BREAK_HTML)
            current_page_chars = 0

        clause_parts = []
        if pasal:
            clause_parts.append(f'<h2 class="ed-clause-heading">{html.escape(pasal)}</h2>')
        if isi:
            for line in isi.splitlines():
                lt = line.strip()
                if lt:
                    if current_page_chars > TARGET_PAGE_CHARS and len(clause_parts) > 1:
                        output_parts.extend(clause_parts)
                        output_parts.append(PAGE_BREAK_HTML)
                        clause_parts = []
                        current_page_chars = 0
                    clause_parts.append(f'<p class="ed-doc-p">{html.escape(lt)}</p>')
                    current_page_chars += len(lt)

        if clause_parts:
            output_parts.extend(clause_parts)
            current_page_chars += len(pasal)

    return "\n".join(output_parts)


def test_extract_digital_pdf():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pdf_path = os.path.join(base_dir, "data", "contracts", "5300e764-f40b-44a6-9320-b33a74de8174.pdf")
    res = extract_pdf_test(pdf_path)
    assert len(res) > 100
    assert "ed-doc-p" in res
    plain = re.sub(r"<[^>]+>", "", res).strip()
    assert len(plain) >= 40


def test_extract_scanned_pdf_returns_empty_not_fake_page_breaks():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pdf_path = os.path.join(base_dir, "data", "contracts", "04fcebf2-e383-4c75-ab80-fa23989d00a0.pdf")
    res = extract_pdf_test(pdf_path)
    # Scanned PDF with zero text should return empty string so fallback triggers
    assert res == ""
    assert "ed-page-break" not in res


def test_reconstruct_from_results_json_seamless_pagination():
    mock_clauses = []
    # Create 12 clauses of ~450 chars each -> total ~5400 chars (approx 3 sheets)
    for i in range(1, 13):
        mock_clauses.append({
            "pasal": f"Pasal {i}: Ketentuan Layanan {i}",
            "isi_pasal": f"1. Ini adalah ayat pertama dari pasal {i} yang mengatur hak dan kewajiban secara lengkap.\n"
                         f"2. Seluruh kewajiban pada pasal {i} harus dipatuhi oleh kedua belah pihak tanpa terkecuali.\n"
                         f"3. Pelanggaran terhadap pasal {i} akan dikenakan penalti sesuai ketentuan yang berlaku."
        })

    mock_data = {
        "summary": {
            "jenis_dokumen": "Perjanjian Kerja Sama (PKS)",
            "pokok_perjanjian": "Layanan Integrasi Cloud",
            "pihak_pertama": "PT Solusi A",
            "pihak_kedua": "PT Mitra B",
        },
        "results": mock_clauses,
    }

    html_out = reconstruct_from_results_json(mock_data, "Perjanjian_Komp.pdf")
    assert '<h1 class="ed-paper-title">Perjanjian Kerja Sama (PKS)</h1>' in html_out
    assert 'Layanan Integrasi Cloud' in html_out

    # Must contain seamless page breaks
    pb_count = html_out.count("ed-page-break")
    assert pb_count >= 1, f"Expected page breaks for 5400 char contract, got {pb_count}"

    # Verify that a page break is always followed cleanly by a clause heading, never orphaning headings
    sheets = html_out.split('<div class="ed-page-break"')
    for idx, sheet in enumerate(sheets[1:], start=2):
        trimmed = sheet.strip()
        assert trimmed.startswith('contenteditable="false"') or trimmed.startswith('>'), f"Bad page break element syntax in sheet {idx}"
        # Heading should appear right near top of the new sheet
        assert "<h2 class=\"ed-clause-heading\">" in trimmed[:150], f"New sheet {idx} should start cleanly with a clause heading"


def test_blank_and_unpaginated_draft_detection():
    # 1. Blank dummy detection
    empty_breaks = '<div class="ed-page-break" data-page-break="true"></div>\n<div class="ed-page-break"></div>'
    plain = re.sub(r"<[^>]+>", "", empty_breaks).replace("\u200b", "").strip()
    assert len(plain) < 40

    # 2. Unpaginated long draft detection for auto-healing
    long_unbroken = "<h1>Judul</h1>" + "<p>Paragraf panjang.</p>" * 150
    has_no_breaks = bool("ed-page-break" not in long_unbroken and len(re.sub(r"<[^>]+>", "", long_unbroken).strip()) >= 2200)
    assert has_no_breaks is True


def test_garbled_ocr_text_detection():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
    from routers.editor import _is_text_garbled, _convert_vlm_text_to_html

    clean_text = (
        "PERJANJIAN KERJASAMA JASA LAYANAN CASH MANAGEMENT SYSTEM "
        "ANTARA PT APLIKANUSA LINTASARTA DAN PT GLOBAL PRIMA TEKNOLOGI. "
        "Perjanjian Kerja Sama antara PT Aplikanusa Lintasarta dan PT Global Prima Teknologi "
        "tentang Komersialisasi Bersama Layanan Cash Management System dibuat pada hari ini "
        "Kamis tanggal Delapan Belas bulan Februari tahun Dua Ribu Dua Puluh Satu di Jakarta."
    )
    assert _is_text_garbled(clean_text) is False

    garbled_ocr_text = (
        "PEN.'ANJIAN KEBJASAMA JASA LAYANAN CASH MANAGEMENT SYSIEM\n"
        "ANTARA P{. APUKANUS,{ IINTASARTA\n"
        "DAi\\t PT. GII)BAL PRIMA TEKNOIOGI\n"
        "Nomor Lintasarta: W7 /.,AnKSlzzC0f.nOZI\n"
        "Nomor GPT: 009/GPT-LAJPKS/IX/2020\n"
        "PerjanjianKerjaSamaantaraPTAplikanusalintasarg.-daSPTGlobalPrimaTeknologitentang\n"
        "Komersialisasi Bersama l-uy-un C\".t -li-\"g\";\"nt\n"
        "System ('Perjanjian') dibuat pada hari ini Kamis tanggal ii\"i.p- li\"f.. U\"f- februari "
        "tahun Oua RibriDua Puluh Sanr (18 Februari 2o2l), di Jakarh\" oleh dan antara:\n"
        "LPT.APLIKANUSALINTASARTA,suatuPergeroanyangdidirikanberdasarkanhukumIndonesia, "
        "- aa\". hal ini diwakili otJ-CnunioO.l,tn selaku oinExTUR bertindak unnrk i- 1ot -y*\" ;;·;;, "
        "berkedudukan di Gedung Menara Thamrin LI 12' Jalan MH Thamrin Kavl 3- ' Jlfarta "
        "Pusat-Indonesia rozso' yan! aidifiUn Jtngu' Aha Nomor: 26 tanggal 4 April 1988' dibuat "
        "dihadapan Mohamad s*ii\"dj;i' sH..il*y: di Jakarta vnng s\"'n\"' pengurusnva telah "
        "mengalami beberapa kali"
    )
    assert _is_text_garbled(garbled_ocr_text) is True


def test_convert_vlm_text_to_html_formatting():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
    from routers.editor import _convert_vlm_text_to_html

    sample_vlm = (
        "PERJANJIAN KERJASAMA LAYANAN\n"
        "Nomor: 001/PKS/2026\n"
        "Perjanjian ini dibuat oleh dan antara para pihak.\n"
        "--- Halaman 2 ---\n"
        "Pasal 1 Ketentuan Umum\n"
        "Ketentuan dalam perjanjian ini mengikat kedua belah pihak.\n"
        "● Pihak pertama berkewajiban menyediakan infrastruktur.\n"
        "● Pihak kedua berkewajiban menjaga kerahasiaan."
    )
    html_out = _convert_vlm_text_to_html(sample_vlm)
    assert '<h1 class="ed-paper-title"' in html_out
    assert 'PERJANJIAN KERJASAMA LAYANAN' in html_out
    assert '<p class="ed-doc-meta"' in html_out
    assert 'ed-page-break' in html_out
    assert '<h2 class="ed-clause-heading">Pasal 1 Ketentuan Umum</h2>' in html_out
    assert '<p class="ed-doc-bullet">Pihak pertama berkewajiban' in html_out


def test_convert_vlm_text_to_html_with_tables_and_signatures():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
    from routers.editor import _convert_vlm_text_to_html

    sample_vlm = (
        "LAMPIRAN 2: PROSEDUR ESKALASI MASALAH PRODUKSI\n"
        "### [Diagram Alir: Prosedur Pelaporan dan Eskalasi]\n"
        "**Swimlane / Pihak Terkait**: Bank's Client | Lintasarta/GPT Support Team | GPT Help Desk\n"
        "1. Nasabah Bank melapor ke HelpDesk Lintasarta.\n"
        "2. Tim IT Support memfilter insiden.\n\n"
        "| Tahapan | Pelaksana | Batas Waktu |\n"
        "| --- | --- | --- |\n"
        "| Triage Insiden | Tim Helpdesk | 15 Menit |\n"
        "| Eskalasi Masalah | Tim GPT Support | 1 Jam |\n\n"
        "[Paraf: LINTASARTA | GPT]\n"
        "[Tanda Tangan: PT Global Prima Teknologi - Milton Edmond S.]\n"
    )
    html_out = _convert_vlm_text_to_html(sample_vlm)

    # 1. Heading & Workflow formatting
    assert '<h2 class="ed-clause-heading">LAMPIRAN 2: PROSEDUR ESKALASI MASALAH PRODUKSI</h2>' in html_out
    assert '<div class="ed-doc-workflow-header">' in html_out
    assert 'Prosedur Pelaporan dan Eskalasi' in html_out
    assert '<div class="ed-doc-workflow-swimlane">' in html_out

    # 2. Markdown Table conversion to HTML table
    assert '<table class="ed-doc-table">' in html_out
    assert '<th>Tahapan</th>' in html_out
    assert '<th>Batas Waktu</th>' in html_out
    assert '<td>Triage Insiden</td>' in html_out
    assert '<td>15 Menit</td>' in html_out

    # 3. Signature & Paraf Badges
    assert '<div class="ed-doc-signature-badge" data-signature="true">[Paraf: LINTASARTA | GPT]</div>' in html_out
    assert '<div class="ed-doc-signature-badge" data-signature="true">[Tanda Tangan: PT Global Prima Teknologi - Milton Edmond S.]</div>' in html_out


def test_extract_pasal_items_with_annexes_and_high_counts():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
    from services.rag_service import extract_pasal_items

    # Build a simulated 28-section legal document
    doc_sections = []
    for i in range(1, 26):
        doc_sections.append(f"Pasal {i}\nIni adalah isi lengkap dari ketentuan pasal {i} yang mengatur operasional dan hak para pihak secara mengikat.")
    doc_sections.append("Lampiran 1: Service Level Agreement (SLA)\nKetersediaan layanan cloud minimal 99.9% setiap bulan kalender.")
    doc_sections.append("Lampiran 2: Prosedur Eskalasi\nSetiap insiden kritis harus dieskalasi ke tim teknis dalam waktu 30 menit.")
    doc_sections.append("Ketentuan Penutup\nPerjanjian ini berlaku efektif sejak tanggal penandatanganan oleh para pihak.")

    full_contract_text = "\n\n".join(doc_sections)

    items = extract_pasal_items(full_contract_text, max_items=80)

    # Should capture all 25 articles + 2 lampiran + ketentuan penutup = 28 items (not capped at 20!)
    assert len(items) == 28, f"Expected 28 extracted sections, got {len(items)}"

    # Check that articles beyond 20 are present
    assert items[0]["pasal"] == "Pasal 1"
    assert items[20]["pasal"] == "Pasal 21"
    assert items[24]["pasal"] == "Pasal 25"

    # Check that Lampiran and Ketentuan Penutup are captured
    labels = [it["pasal"] for it in items]
    assert any("Lampiran 1" in lbl for lbl in labels), "Lampiran 1 was not extracted"
    assert any("Lampiran 2" in lbl for lbl in labels), "Lampiran 2 was not extracted"
    assert any("Ketentuan Penutup" in lbl for lbl in labels), "Ketentuan Penutup was not extracted"


def test_docx_extraction_preserves_formatting_and_tables():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
    from routers.editor import _extract_docx_to_html, _is_text_garbled

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docx1 = os.path.join(base_dir, "data", "contracts", "f48b0e35-21de-471f-8727-f21e576e85df.docx")
    docx2 = os.path.join(base_dir, "data", "contracts", "2823fbc8-93b0-4ccb-83de-875e1e0e4d3d.docx")

    if os.path.exists(docx1):
        html1 = _extract_docx_to_html(docx1)
        assert len(html1) > 4000, f"Expected full DOCX HTML (>4000 chars), got {len(html1)}"
        assert '<h1 class="ed-paper-title"' in html1
        assert '<h2 class="ed-clause-heading"' in html1
        assert '<table class="ed-doc-table">' in html1
        for p_num in range(1, 9):
            assert f"PASAL {p_num}" in html1.upper(), f"Missing Pasal {p_num} in extracted DOCX HTML"
        plain1 = re.sub(r"<[^>]+>", "", html1).strip()
        assert _is_text_garbled(plain1) is False

    if os.path.exists(docx2):
        html2 = _extract_docx_to_html(docx2)
        assert len(html2) > 2500, f"Expected full DOCX HTML (>2500 chars), got {len(html2)}"
        assert '<h1 class="ed-paper-title"' in html2
        assert '<h2 class="ed-clause-heading"' in html2
        assert html2.count('<table class="ed-doc-table">') == 5
        assert _is_text_garbled(html2) is False
        # Verify high-fidelity styling & line-break preservation
        assert "background-color:#1B365D" in html2
        assert "PT NUSANTARA SIBER INDONESIA<br/>" in html2
        assert "NOMOR DOKUMEN<br/>" in html2
        assert "<u>Ir. Hendra Wijaya, M.Kom" in html2

        from services.html_docx import build_docx
        rt_doc = build_docx(html2)
        assert len(rt_doc.tables) == 5
        # Header banner cell keeps navy background and line break between lines
        hdr_cell_xml = rt_doc.tables[0].cell(0, 0)._tc.xml
        assert 'w:fill="1B365D"' in hdr_cell_xml
        assert "<w:br/>" in hdr_cell_xml
        # Signature cell keeps underlined signatory name and multi-line spacing
        sig_cell_xml = rt_doc.tables[4].cell(0, 0)._tc.xml
        assert "<w:u " in sig_cell_xml
        assert sig_cell_xml.count("<w:br/>") >= 5


def test_extract_pasal_items_does_not_split_inline_references():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api"))
    from services.rag_service import extract_pasal_items

    sample_contract = (
        "PASAL 1\n"
        "RUANG LINGKUP KERJA SAMA\n"
        "1. Para Pihak sepakat melaksanakan kerja sama sesuai spesifikasi yang tercantum dalam\n"
        "Lampiran I yang merupakan bagian tidak terpisahkan dari Perjanjian ini.\n"
        "2. Apabila terdapat perubahan ketentuan, maka Para Pihak akan mengacu pada\n"
        "Pasal yang mengatur tentang Addendum dan Perubahan.\n\n"
        "PASAL 2\n"
        "JANGKA WAKTU PERJANJIAN\n"
        "Perjanjian ini berlaku selama 24 (dua puluh empat) bulan terhitung sejak tanggal penandatanganan."
    )
    items = extract_pasal_items(sample_contract)
    assert len(items) == 2, f"Expected exactly 2 Pasals without inline split, got {len(items)}: {[i['pasal'] for i in items]}"
    assert "Lampiran I yang merupakan" in items[0]["deskripsi"]
    assert "Pasal yang mengatur" in items[0]["deskripsi"]



