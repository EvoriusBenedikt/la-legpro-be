"""Generate a structured, selectable multi-page PDF Legal Audit Report using PyMuPDF Story."""
from __future__ import annotations

from datetime import datetime
import html as html_lib
import io
import re
from typing import Any, Optional

import fitz  # PyMuPDF

_SEG_RE = re.compile(r"\[seg_\d+\]\s*", re.IGNORECASE)


def _clean_text(raw: Any) -> str:
    if not raw:
        return ""
    txt = _SEG_RE.sub("", str(raw)).strip()
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    return html_lib.escape(txt).replace("\n", "<br/>")


def _status_badge_style(status: str) -> tuple[str, str]:
    st = (status or "").upper()
    if st == "FATAL":
        return "#991b1b", "#fee2e2"
    if st == "BERESIKO":
        return "#92400e", "#fef3c7"
    if st == "SESUAI":
        return "#065f46", "#d1fae5"
    return "#374151", "#f3f4f6"


def _status_label(status: str, is_en: bool) -> str:
    st = (status or "").upper()
    if is_en:
        return {
            "FATAL": "FATAL",
            "BERESIKO": "AT RISK",
            "SESUAI": "COMPLIANT",
            "TIDAK DIATUR": "NOT REGULATED",
        }.get(st, st or "NOT REGULATED")
    return st or "TIDAK DIATUR"


def _pos_label(pos: str, is_en: bool) -> str:
    p = (pos or "").upper()
    if is_en:
        return {
            "MENGUNTUNGKAN": "+ Favourable",
            "MERUGIKAN": "- Unfavourable",
            "NETRAL": "= Neutral",
        }.get(p, "")
    return {
        "MENGUNTUNGKAN": "+ Menguntungkan",
        "MERUGIKAN": "- Merugikan",
        "NETRAL": "= Netral",
    }.get(p, "")


def _render_bullets(items: list[Any], prefix: str, color: str) -> str:
    if not items:
        return ""
    rows: list[str] = []
    for it in items:
        if isinstance(it, dict):
            poin = _clean_text(it.get("poin") or "")
            alasan = _clean_text(it.get("alasan") or "")
            line = f"<b>{poin}</b>" + (f" — {alasan}" if alasan else "")
        else:
            line = _clean_text(it)
        if line:
            rows.append(f'<div style="color:{color}; margin-bottom:3pt;">{prefix} {line}</div>')
    return "".join(rows)


def build_analysis_report_html(
    filename: str,
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    missing_clauses: Optional[list[dict[str, Any]]] = None,
    locale: str = "id",
) -> str:
    """Assemble the 3-section Legal Audit Brief HTML for fitz.Story."""
    is_en = (locale or "").lower().startswith("en")
    summary = summary or {}
    results = results or []
    missing_clauses = missing_clauses or []

    fatal_list = [r for r in results if (r.get("status") or "").upper() == "FATAL"]
    risk_list = [r for r in results if (r.get("status") or "").upper() == "BERESIKO"]
    sesuai_list = [r for r in results if (r.get("status") or "").upper() == "SESUAI"]
    unreg_list = [
        r for r in results
        if (r.get("status") or "").upper() not in ("FATAL", "BERESIKO", "SESUAI")
    ]
    action_list = fatal_list + risk_list
    other_list = sesuai_list + unreg_list

    regulated_cnt = len(fatal_list) + len(risk_list) + len(sesuai_list)
    score = summary.get("skor_kepatuhan")
    if score is None:
        score = 100 if regulated_cnt == 0 else round((len(sesuai_list) / max(regulated_cnt, 1)) * 100)

    fav_cnt = sum(1 for r in results if (r.get("posisi_perusahaan") or "").upper() == "MENGUNTUNGKAN")
    unfav_cnt = sum(1 for r in results if (r.get("posisi_perusahaan") or "").upper() == "MERUGIKAN")
    neu_cnt = max(0, len(results) - fav_cnt - unfav_cnt)
    pos_score = summary.get("skor_posisi")
    if pos_score is None:
        pos_score = round(((fav_cnt - unfav_cnt) / max(len(results), 1)) * 100)

    doc_type = _clean_text(summary.get("jenis_dokumen") or ("Contract" if is_en else "Kontrak"))
    sektor = _clean_text(summary.get("sektor_bisnis") or ("General" if is_en else "Umum"))
    pihak_1 = _clean_text(summary.get("pihak_pertama") or "—")
    pihak_2 = _clean_text(summary.get("pihak_kedua") or "—")
    pokok = _clean_text(summary.get("pokok_perjanjian") or "")
    exp_date = _clean_text(summary.get("tanggal_berakhir") or ("Not Detected" if is_en else "Tidak Terdeteksi"))
    gen_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Section 1: Action Required (Fatal & At-Risk Clauses)
    sec1_cards: list[str] = []
    for idx, r in enumerate(action_list, start=1):
        st_fg, st_bg = _status_badge_style(r.get("status", ""))
        st_lbl = _status_label(r.get("status", ""), is_en)
        pos_lbl = _pos_label(r.get("posisi_perusahaan", ""), is_en)
        pasal_title = _clean_text(r.get("pasal") or f"Clause {idx}")
        citation = _clean_text(r.get("pasal_regulasi_terkait") or "")
        orig_text = _clean_text(r.get("isi_pasal") or "")
        penjelasan = _clean_text(r.get("penjelasan") or r.get("ai_analysis") or "")
        redline = _clean_text(r.get("rekomendasi") or r.get("usulan_revisi") or "")
        pros_html = _render_bullets(r.get("kelebihan") or [], "+", "#065f46")
        cons_html = _render_bullets(r.get("kekurangan") or [], "−", "#991b1b")

        sup_regs = r.get("supporting_regulations") or []
        sup_html = ""
        if sup_regs:
            s0 = sup_regs[0]
            s_ref = _clean_text(f"{s0.get('jenis', '')} {s0.get('nomor', '')}".strip())
            s_txt = _clean_text((s0.get("teks") or "")[:360])
            if s_ref or s_txt:
                sup_html = (
                    f'<div style="margin-top:6pt; padding:6pt; background-color:#f8fafc; border-left:2pt solid #64748b; font-size:8.5pt;">'
                    f'<b>{"Regulatory Excerpt" if is_en else "Kutipan Regulasi"} ({s_ref}):</b> {s_txt}</div>'
                )

        sec1_cards.append(
            f'<div style="border:1pt solid #e2e8f0; padding:10pt; margin-bottom:10pt; background-color:#ffffff;">'
            f'  <div style="margin-bottom:6pt;">'
            f'    <span style="background-color:{st_bg}; color:{st_fg}; font-weight:bold; font-size:8.5pt; padding:2pt 6pt;">[{st_lbl}]</span> '
            f'    <b style="font-size:10.5pt; color:#0f172a;">{pasal_title}</b>'
            + (f' <span style="font-size:8.5pt; color:#475569;">({pos_lbl})</span>' if pos_lbl else "")
            + (f'<div style="font-size:8.5pt; color:#1d4ed8; margin-top:2pt;"><b>Ref:</b> {citation}</div>' if citation else "")
            + f'  </div>'
            + (f'  <div style="margin-bottom:6pt; font-size:9pt; color:#1e293b;"><b>{"Risk & Legal Finding" if is_en else "Temuan & Analisis Risiko"}:</b><br/>{penjelasan}</div>' if penjelasan else "")
            + (f'  <div style="margin-bottom:6pt; font-size:8.5pt;">{pros_html}{cons_html}</div>' if (pros_html or cons_html) else "")
            + f'  <table style="width:100%; border-collapse:collapse; margin-top:4pt;">'
            f'    <tr>'
            f'      <td style="width:50%; vertical-align:top; padding:6pt; background-color:#fff1f2; border:1pt solid #fecdd3; font-size:8.5pt;">'
            f'        <b style="color:#9f1239;">{"CURRENT CLAUSE" if is_en else "TEKS KLAUSUL SAAT INI"}</b><br/><br/>{orig_text or "—"}'
            f'      </td>'
            f'      <td style="width:50%; vertical-align:top; padding:6pt; background-color:#ecfdf5; border:1pt solid #a7f3d0; font-size:8.5pt;">'
            f'        <b style="color:#065f46;">{"RECOMMENDED REDLINE" if is_en else "REKOMENDASI REVISI (REDLINE)"}</b><br/><br/>{redline or "—"}'
            f'      </td>'
            f'    </tr>'
            f'  </table>'
            + sup_html
            + f'</div>'
        )

    sec1_html = (
        "".join(sec1_cards)
        if sec1_cards
        else f'<p style="font-size:9pt; color:#065f46;">{"No Fatal or At-Risk clauses detected." if is_en else "Tidak ditemukan klausul Fatal atau Beresiko."}</p>'
    )

    # Section 2: Missing Clause Gap Analysis
    gap_cards: list[str] = []
    for g in missing_clauses:
        g_title = _clean_text(g.get("title") or g.get("clause_name") or g.get("name") or "Klausul")
        g_sev = _clean_text((g.get("severity") or g.get("priority") or "IMPORTANT").upper())
        g_why = _clean_text(g.get("reason") or g.get("explanation") or g.get("description") or "")
        g_draft = _clean_text(g.get("suggested_clause") or g.get("template_text") or g.get("recommendation") or "")
        gap_cards.append(
            f'<div style="border:1pt solid #e2e8f0; padding:8pt; margin-bottom:8pt;">'
            f'  <div style="font-size:9.5pt;"><b>[{g_sev}] {g_title}</b></div>'
            + (f'  <div style="font-size:8.5pt; color:#334155; margin-top:3pt;">{g_why}</div>' if g_why else "")
            + (f'  <div style="font-size:8.5pt; color:#065f46; background-color:#f0fdf4; padding:5pt; margin-top:4pt;"><b>{"Suggested Clause" if is_en else "Usulan Klausul"}:</b><br/>{g_draft}</div>' if g_draft else "")
            + f'</div>'
        )
    sec2_html = (
        f'<h2 style="font-size:12pt; color:#0f172a; border-bottom:1pt solid #cbd5e1; padding-bottom:3pt; margin-top:14pt;">'
        f'2. {"Missing Clause Gap Analysis" if is_en else "Analisis Kesenjangan Klausul (Gap Analysis)"} ({len(gap_cards)})</h2>'
        + "".join(gap_cards)
        if gap_cards
        else ""
    )

    # Section 3: Complete Clause Log (Compliant & Unregulated)
    sec3_rows: list[str] = []
    for idx, r in enumerate(other_list, start=1):
        st_fg, st_bg = _status_badge_style(r.get("status", ""))
        st_lbl = _status_label(r.get("status", ""), is_en)
        pos_lbl = _pos_label(r.get("posisi_perusahaan", ""), is_en)
        pasal_title = _clean_text(r.get("pasal") or f"Clause {idx}")
        orig_text = _clean_text(r.get("isi_pasal") or "")
        penjelasan = _clean_text(r.get("penjelasan") or "")
        usulan_komersial = _clean_text(r.get("usulan_revisi") or "")
        pros_html = _render_bullets(r.get("kelebihan") or [], "+", "#065f46")
        cons_html = _render_bullets(r.get("kekurangan") or [], "−", "#991b1b")

        sec3_rows.append(
            f'<div style="border-bottom:1pt solid #e2e8f0; padding:7pt 0;">'
            f'  <div style="margin-bottom:3pt;">'
            f'    <span style="background-color:{st_bg}; color:{st_fg}; font-weight:bold; font-size:8pt; padding:1pt 5pt;">[{st_lbl}]</span> '
            f'    <b style="font-size:9.5pt; color:#0f172a;">{pasal_title}</b>'
            + (f' <span style="font-size:8pt; color:#475569;">({pos_lbl})</span>' if pos_lbl else "")
            + f'  </div>'
            f'  <div style="font-size:8.5pt; color:#334155; margin-bottom:3pt;">{orig_text}</div>'
            + (f'  <div style="font-size:8pt; color:#475569; margin-bottom:2pt;">{penjelasan}</div>' if penjelasan else "")
            + (f'  <div style="font-size:8pt;">{pros_html}{cons_html}</div>' if (pros_html or cons_html) else "")
            + (f'  <div style="font-size:8pt; color:#065f46; margin-top:2pt;"><b>{"Commercial Revision" if is_en else "Usulan Revisi Komersial"}:</b> {usulan_komersial}</div>' if usulan_komersial else "")
            + f'</div>'
        )

    sec3_num = "3" if gap_cards else "2"
    sec3_html = (
        f'<h2 style="font-size:12pt; color:#0f172a; border-bottom:1pt solid #cbd5e1; padding-bottom:3pt; margin-top:14pt;">'
        f'{sec3_num}. {"Complete Clause Log (Compliant & Unregulated)" if is_en else "Daftar Lengkap Klausul (Sesuai & Tidak Diatur)"} ({len(other_list)})</h2>'
        + ("".join(sec3_rows) if sec3_rows else f'<p style="font-size:8.5pt; color:#64748b;">—</p>')
    )

    return f"""
    <html>
    <body style="font-family: sans-serif; color: #0f172a; line-height: 1.4;">
      <div style="border-bottom: 2pt solid #1e3a8a; padding-bottom: 8pt; margin-bottom: 10pt;">
        <div style="font-size: 8pt; color: #1e3a8a; font-weight: bold; letter-spacing: 0.5pt;">
          {"LEGAL COMPLIANCE & COMMERCIAL AUDIT REPORT" if is_en else "LAPORAN AUDIT KEPATUHAN HUKUM & POSISI KOMERSIAL"} · {gen_date}
        </div>
        <h1 style="font-size: 15pt; margin: 3pt 0; color: #0f172a;">{_clean_text(filename)}</h1>
        <div style="font-size: 8.5pt; color: #475569;">
          <b>{"Type" if is_en else "Jenis"}:</b> {doc_type} &nbsp;|&nbsp;
          <b>{"Sector" if is_en else "Sektor"}:</b> {sektor} &nbsp;|&nbsp;
          <b>{"Expires" if is_en else "Berakhir"}:</b> {exp_date}
        </div>
        <div style="font-size: 8.5pt; color: #334155; margin-top: 2pt;">
          <b>{"Parties" if is_en else "Para Pihak"}:</b> {pihak_1} &nbsp;↔&nbsp; {pihak_2}
        </div>
        {f'<div style="font-size: 8.5pt; color: #334155; margin-top: 3pt;">{pokok}</div>' if pokok else ''}
      </div>

      <table style="width: 100%; border-collapse: collapse; margin-bottom: 12pt;">
        <tr>
          <td style="width: 50%; padding: 8pt; background-color: #f8fafc; border: 1pt solid #cbd5e1; vertical-align: top;">
            <div style="font-size: 8pt; font-weight: bold; color: #475569;">
              {"COMPLIANCE SCORE" if is_en else "SKOR KEPATUHAN"}
            </div>
            <div style="font-size: 18pt; font-weight: bold; color: #1e3a8a; margin: 2pt 0;">{score}%</div>
            <div style="font-size: 8.5pt; color: #334155;">
              <b style="color:#991b1b;">{len(fatal_list)} Fatal</b> ·
              <b style="color:#92400e;">{len(risk_list)} {"At Risk" if is_en else "Beresiko"}</b> ·
              <b style="color:#065f46;">{len(sesuai_list)} {"Compliant" if is_en else "Sesuai"}</b> ·
              <span>{len(unreg_list)} {"Not Regulated" if is_en else "Tidak Diatur"}</span>
            </div>
          </td>
          <td style="width: 50%; padding: 8pt; background-color: #f8fafc; border: 1pt solid #cbd5e1; vertical-align: top;">
            <div style="font-size: 8pt; font-weight: bold; color: #475569;">
              {"COMMERCIAL POSITION" if is_en else "POSISI KOMERSIAL"}
            </div>
            <div style="font-size: 18pt; font-weight: bold; color: #0f172a; margin: 2pt 0;">{pos_score:+d}</div>
            <div style="font-size: 8.5pt; color: #334155;">
              <b style="color:#065f46;">+{fav_cnt} {"Favourable" if is_en else "Menguntungkan"}</b> ·
              <b style="color:#991b1b;">−{unfav_cnt} {"Unfavourable" if is_en else "Merugikan"}</b> ·
              <span>={neu_cnt} {"Neutral" if is_en else "Netral"}</span>
            </div>
          </td>
        </tr>
      </table>

      <h2 style="font-size: 12pt; color: #0f172a; border-bottom: 1pt solid #cbd5e1; padding-bottom: 3pt;">
        1. {"Action Required — Fatal & At-Risk Clauses" if is_en else "Perlu Tindakan — Klausul Fatal & Beresiko"} ({len(action_list)})
      </h2>
      {sec1_html}
      {sec2_html}
      {sec3_html}
    </body>
    </html>
    """


def generate_analysis_pdf(
    filename: str,
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    missing_clauses: Optional[list[dict[str, Any]]] = None,
    locale: str = "id",
) -> bytes:
    """Render the Legal Audit Brief HTML into an A4 multi-page PDF with page numbers."""
    html_str = build_analysis_report_html(
        filename=filename,
        summary=summary,
        results=results,
        missing_clauses=missing_clauses,
        locale=locale,
    )

    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (36, 36, -36, -42)
    story = fitz.Story(html=html_str)

    more = 1
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()

    doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
    total_pages = len(doc)
    is_en = (locale or "").lower().startswith("en")
    for idx, page in enumerate(doc, start=1):
        footer_left = "CONFIDENTIAL — INTERNAL LEGAL REVIEW" if is_en else "RAHASIA — TINJAUAN HUKUM INTERNAL"
        footer_right = f"Page {idx} of {total_pages}" if is_en else f"Halaman {idx} dari {total_pages}"
        page.insert_text(
            fitz.Point(36, mediabox.height - 20),
            footer_left,
            fontsize=7.5,
            color=(0.4, 0.45, 0.52),
        )
        page.insert_text(
            fitz.Point(mediabox.width - 110, mediabox.height - 20),
            footer_right,
            fontsize=7.5,
            color=(0.4, 0.45, 0.52),
        )

    out_bytes = doc.tobytes(deflate=True)
    doc.close()
    return out_bytes
