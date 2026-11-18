from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import json
import uuid
import os
import html
import re
import glob
import fitz
import docx

from services import docx_html
from auth import get_current_user
from services import pg_service
from services import doc_notifications
from services import bilingual_extractor

router = APIRouter(prefix="/api", tags=["editor"])

_TABLES_ENSURED = False


def _sanitize_mojibake(text: str) -> str:
    """Cleans up mojibake where UTF-8 sequences were decoded as Windows-1252/Latin-1."""
    if not text:
        return ""
    replacements = [
        ("\u00e2\u20ac\u0153", "“"),
        ("\u00e2\u20ac\u009d", "”"),
        ("\u00e2\u20ac\ufffd", "”"),
        ("\u00e2\u20ac ", "”"),
        ("\u00e2\u20ac\u02dc", "‘"),
        ("\u00e2\u20ac\u2122", "’"),
        ("\u00e2\u20ac\u201d", "—"),
        ("\u00e2\u20ac\u2013", "–"),
        ("\u00e2\u20ac\u00a6", "…"),
        ("\u00e2\u20ac\u00a2", "•"),
        ("\u00e2\u20ac", "”"),
        ("\u00c2\u00a0", " "),
        ("\u00c2\u00b7", "·"),
        ("\u00c2\u00b1", "±"),
    ]
    for bad, good in replacements:
        text = text.replace(bad, good)
    return text


def _is_text_garbled(text: str) -> bool:
    """Detect if extracted document text is corrupted or poor OCR quality.

    Calibrated for Indonesian and English legal text:
    - Collapses Indonesian digraphs ('ng', 'ny', 'sy', 'kh') into single consonant units
    - Ignores short uppercase acronyms (PT, PKS, SLA, BAST,ppn, etc.) and Roman numerals
    - Flags genuine OCR corruption: mid-word punctuation noise, weird casing, 4+ consonant runs,
      vowel-less lowercase words, and glued words (>= 38 chars).
    """
    if not text or len(text) < 30:
        return False

    clean_text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    words = clean_text.split()
    if not words:
        return False

    VOWELS = set("aeiouAEIOU")
    CONSONANTS = set("bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ")
    ROMAN_RE = re.compile(r"^[IVXLCDM]+$", re.I)
    KNOWN_ABBR = {
        "hlm", "dkk", "dll", "dsb", "dst", "ttd", "tgl", "bln", "thn",
        "no", "rp", "idr", "usd", "ppn", "pph", "npwp", "nib", "oss",
        "pkp", "bumn", "bumd", "mabes", "polri", "tni", "ojk", "bi",
        "pks", "mou", "nda", "sla", "bast", "bapp", "baut", "boq", "tor",
        "kak", "spk", "spmk", "po", "pr", "gr", "ivo", "cms", "gpt", "sft",
        "tci", "smd", "pt", "cv", "tbk", "persero", "sh", "mh", "spn", "mkn",
        "se", "mm", "mkom", "st", "mt", "dr", "ir", "prof", "hj", "kh",
        "ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii", "xiii", "xiv", "xv",
        "xvi", "xvii", "xviii", "xix", "xx",
        "sdm", "hki", "hak", "kbli", "skdp", "siup", "tdp", "domisili",
        "pasal", "ayat", "bab", "huruf", "angka", "lampiran",
    }
    MID_WORD_NOISE_RE = re.compile(
        r"[a-zA-Z][*{}|\\~^;$`]+[a-zA-Z]"
        r"|[a-zA-Z]\.[a-zA-Z]\.[a-zA-Z]"
        r"|[a-zA-Z]['\"!?,:;][a-zA-Z]{2,}"
    )
    WEIRD_CASE_RE = re.compile(r"^[a-z]+[A-Z][a-zA-Z]*|[a-z][A-Z]{2,}|[A-Z][a-z]+[A-Z][a-z]+[A-Z]")

    cluster_words = 0
    no_vowel_words = 0
    glued_words = 0
    noise_words = 0
    alpha_word_count = 0

    for w in words:
        clean_edge = w.strip(".,;:!?()[]\"'“”‘’-–—/_•●*0123456789")
        if not clean_edge:
            continue

        if len(clean_edge) >= 38 and "/" not in clean_edge and "@" not in clean_edge and "http" not in clean_edge.lower():
            glued_words += 1

        alpha_str = "".join(c for c in clean_edge if c.isalpha())
        if len(alpha_str) < 3:
            continue

        alpha_word_count += 1
        lower_alpha = alpha_str.lower()

        if MID_WORD_NOISE_RE.search(clean_edge) or WEIRD_CASE_RE.search(clean_edge):
            noise_words += 1

        is_acronym = (alpha_str.isupper() and len(alpha_str) <= 5) or bool(ROMAN_RE.match(alpha_str)) or (lower_alpha in KNOWN_ABBR)

        if not is_acronym:
            if len(alpha_str) >= 4 and not any(c in VOWELS for c in alpha_str):
                no_vowel_words += 1

            collapsed = re.sub(r"(ng|ny|sy|kh)", "x", lower_alpha)
            run = max_run = 0
            for c in collapsed:
                run = run + 1 if c in CONSONANTS else 0
                max_run = max(max_run, run)
            if max_run >= 4:
                cluster_words += 1

    total_words = max(alpha_word_count, 1)
    cluster_ratio = cluster_words / total_words
    no_vowel_ratio = no_vowel_words / total_words
    noise_ratio = noise_words / total_words
    glued_ratio = glued_words / total_words

    alpha_chars = [c for c in text if c.isalpha()]
    vowel_ratio = (
        sum(1 for c in alpha_chars if c in VOWELS) / len(alpha_chars)
        if alpha_chars else 0.0
    )

    reasons = []
    if no_vowel_ratio > 0.04:
        reasons.append(f"no-vowel-words={no_vowel_ratio:.2f}")
    if cluster_ratio > 0.05:
        reasons.append(f"cluster_ratio={cluster_ratio:.2f}")
    if vowel_ratio < 0.22:
        reasons.append(f"vowel_ratio={vowel_ratio:.2f}")
    # Glued words are a ratio, not a raw count: PyMuPDF normally glues a few
    # spans together on long legal PDFs, so a handful is not corruption.
    if glued_ratio > 0.02:
        reasons.append(f"glued_words={glued_words} (ratio={glued_ratio:.2f})")
    if noise_ratio > 0.03:
        reasons.append(f"noise_ratio={noise_ratio:.2f}")

    # Require two or more independent signals before declaring text garbled.
    # A single borderline ratio false-positives on clean Indonesian legal text
    # and wrongly diverts extraction into the synthetic clause reconstruction.
    if len(reasons) >= 2:
        print(f"[Editor] Garbled text detected: {', '.join(reasons)}")
        return True
    if reasons:
        print(f"[Editor] Garble check passed (single weak signal ignored): {', '.join(reasons)}")
    return False


def _ensure_editor_tables() -> None:
    global _TABLES_ENSURED
    if _TABLES_ENSURED:
        return
    try:
        pg_service.execute(
            """
            CREATE TABLE IF NOT EXISTS document_editor_drafts (
                id           TEXT PRIMARY KEY,
                report_id    TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                user_id      TEXT NOT NULL,
                content_html TEXT NOT NULL,
                content_json TEXT,
                version      INT NOT NULL DEFAULT 1,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_editor_drafts_report ON document_editor_drafts (report_id);

            CREATE TABLE IF NOT EXISTS document_editor_comments (
                id            TEXT PRIMARY KEY,
                report_id     TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                user_id       TEXT NOT NULL,
                author_name   TEXT NOT NULL,
                selected_text TEXT NOT NULL,
                comment_text  TEXT NOT NULL,
                replies_json  TEXT NOT NULL DEFAULT '[]',
                resolved      BOOLEAN NOT NULL DEFAULT false,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_editor_comments_report ON document_editor_comments (report_id, created_at);

            CREATE TABLE IF NOT EXISTS document_editor_versions (
                id             TEXT PRIMARY KEY,
                report_id      TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                version_number INT NOT NULL,
                title          TEXT NOT NULL,
                snapshot_html  TEXT NOT NULL,
                created_by     TEXT NOT NULL,
                author_name    TEXT NOT NULL DEFAULT 'Reviewer',
                color_index    INT NOT NULL DEFAULT 0,
                changes_json   TEXT NOT NULL DEFAULT '[]',
                created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_editor_versions_report ON document_editor_versions (report_id, version_number DESC);

            CREATE TABLE IF NOT EXISTS workspace_collaborators (
                id          TEXT PRIMARY KEY,
                notebook_id TEXT NOT NULL,
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                invited_by  TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'editor',
                color_index INT NOT NULL DEFAULT 1,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (notebook_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ws_collab_notebook ON workspace_collaborators (notebook_id);
            CREATE INDEX IF NOT EXISTS idx_ws_collab_user ON workspace_collaborators (user_id);

            CREATE TABLE IF NOT EXISTS workspace_presence (
                report_id     TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
                user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                username      TEXT NOT NULL,
                color_index   INT NOT NULL DEFAULT 0,
                is_typing     BOOLEAN NOT NULL DEFAULT false,
                active_clause TEXT,
                last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (report_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ws_presence_report ON workspace_presence (report_id, last_seen DESC);
            """
        )
        _TABLES_ENSURED = True
    except Exception as e:
        print(f"[Editor] Table initialization warning: {e}")


class DraftSaveRequest(BaseModel):
    content_html: str
    content_json: Optional[dict] = None


class CollaboratorInviteRequest(BaseModel):
    identifier: str


class EditorHeartbeatRequest(BaseModel):
    is_typing: bool = False
    active_clause: Optional[str] = None
    known_updated_at: Optional[str] = None


class CreateVersionRequest(BaseModel):
    content_html: str
    title: Optional[str] = None
    changes: Optional[list] = None
    clean_highlights: Optional[bool] = False


class CommentCreateRequest(BaseModel):
    id: Optional[str] = None
    selected_text: str
    comment_text: str


class CommentReplyRequest(BaseModel):
    reply_text: str


class CommentResolveRequest(BaseModel):
    resolved: bool


class ExportDocxRequest(BaseModel):
    content_html: str
    bilingual_view: Optional[str] = "both"


class AiAssistRequest(BaseModel):
    clause_text: str = ""
    preset: str = "compliance"
    recommendation: Optional[str] = None
    mode: Optional[str] = "rewrite"
    clause_title: Optional[str] = None
    advisory_guidance: Optional[str] = None
    recommended_elements: Optional[list[str]] = None
    user_deal_terms: Optional[str] = None
    is_bilingual: Optional[bool] = False


class MissingClauseStatusRequest(BaseModel):
    resolution_status: str
    dismissed_reason: Optional[str] = None


class RecheckGapsRequest(BaseModel):
    contract_type: str


class RecheckRequest(BaseModel):
    content_html: str


def _verify_report_owner(report_id: str, user_id: str) -> dict:
    _ensure_editor_tables()
    row = pg_service.query_one(
        "SELECT id, filename, notebook_id, user_id FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found or you are not the workspace owner")
    return row


def verify_report_access(report_id: str, user_id: str) -> dict:
    """Verifies that the user is either the report owner or an invited workspace collaborator."""
    _ensure_editor_tables()
    row = pg_service.query_one(
        "SELECT id, filename, user_id, notebook_id FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found")

    is_owner = (row["user_id"] == user_id)
    color_index = 0
    if not is_owner:
        nb_id = row.get("notebook_id") or row["id"]
        collab = pg_service.query_one(
            "SELECT id, role, color_index FROM workspace_collaborators WHERE notebook_id = %s AND user_id = %s",
            (nb_id, user_id),
        )
        if not collab:
            raise HTTPException(status_code=404, detail="Document report not found or access denied")
        color_index = collab.get("color_index", 1)

    return {
        "id": row["id"],
        "filename": row["filename"],
        "user_id": user_id,
        "owner_id": row["user_id"],
        "notebook_id": row.get("notebook_id"),
        "is_owner": is_owner,
        "color_index": color_index,
    }


def _serialize_comment(row: dict) -> dict:
    replies = []
    if row.get("replies_json"):
        try:
            replies = json.loads(row["replies_json"])
        except Exception:
            replies = []
    return {
        "id": row["id"],
        "report_id": row["report_id"],
        "author_name": row["author_name"],
        "selected_text": _sanitize_mojibake(row["selected_text"]),
        "comment_text": _sanitize_mojibake(row["comment_text"]),
        "replies": replies,
        "resolved": bool(row["resolved"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _contract_file_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data",
        "contracts",
    )


def _find_contract_file(report_id: str, stored_filename: str = "") -> Optional[str]:
    base_dir = _contract_file_dir()
    if not os.path.isdir(base_dir):
        return None

    # 1. Exact path using stored_filename extension if available
    if stored_filename:
        ext = os.path.splitext(stored_filename)[1].lower()
        if ext:
            direct_path = os.path.join(base_dir, f"{report_id}{ext}")
            if os.path.exists(direct_path):
                return direct_path

    # 2. Check any existing file in data/contracts with report_id.*
    matches = glob.glob(os.path.join(base_dir, f"{report_id}.*"))
    if matches:
        priority = {".docx": 1, ".doc": 2, ".pdf": 3, ".txt": 4}
        matches.sort(key=lambda x: priority.get(os.path.splitext(x)[1].lower(), 99))
        return matches[0]

    # 3. Check data/pdfs as fallback if stored_filename exists
    if stored_filename:
        alt_base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
            "pdfs",
        )
        if os.path.isdir(alt_base):
            alt_path = os.path.join(alt_base, stored_filename)
            if os.path.exists(alt_path):
                return alt_path

    return None


def _contract_file_path(report_id: str, stored_filename: str) -> str:
    found = _find_contract_file(report_id, stored_filename)
    if found:
        return found
    ext = os.path.splitext(stored_filename)[1].lower() if stored_filename else ""
    return os.path.join(_contract_file_dir(), f"{report_id}{ext}")


_IMG_MAX_COUNT = 12          # images kept per document
_IMG_MAX_B64_CHARS = 3_000_000  # total base64 budget per document (draft is a TEXT column, PUT body)


def _pdf_table_html(rows: list, borderless: bool = False) -> str:
    """Editor table markup (same classes as the DOCX extractor / insert-table)."""
    is_bi, col_order = bilingual_extractor.detect_bilingual_columns(rows)
    if is_bi and col_order:
        table_html, _ = bilingual_extractor.render_bilingual_table_html(
            rows, col_order, borderless=borderless
        )
        return table_html

    def cell(c):
        return html.escape(str(c or "").strip()).replace("\n", "<br/>")
    out = ['<table class="ed-doc-table"><thead><tr>']
    out += [f"<th>{cell(c)}</th>" for c in rows[0]]
    out.append("</tr></thead><tbody>")
    for row in rows[1:]:
        out.append("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _render_clip_img(page, rect, budget: dict) -> str:
    """Render a page region as a compressed inline <img>; '' when over the count/size budget."""
    if budget["n"] >= _IMG_MAX_COUNT:
        return ""
    try:
        rect = fitz.Rect(rect) & page.rect
        if rect.is_empty:
            return ""
        dpi = max(40, min(110, int(800 / (rect.width / 72))))  # cap output at ~800px wide
        pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
        import base64
        b64 = base64.b64encode(pix.tobytes("jpeg", jpg_quality=65)).decode()
    except Exception:
        return ""
    if budget["b64"] + len(b64) > _IMG_MAX_B64_CHARS:
        return ""
    budget["n"] += 1
    budget["b64"] += len(b64)
    return f'<p class="ed-doc-p"><img src="data:image/jpeg;base64,{b64}" alt="" style="max-width:100%;"/></p>'


def _pdf_page_graphics(page, text_blocks, table_bboxes, budget: dict) -> list:
    """(y, x, html) for embedded images and vector-drawn regions (signatures) not covered by text/tables."""
    pr = page.rect
    found = []

    def ok(r):
        return (r.width >= 40 and r.height >= 20 and r.width * r.height < pr.width * pr.height * 0.5
                and not any(fitz.Rect(t).intersects(r) for t in table_bboxes))

    try:
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"])
            if ok(r):  # full-page scans/backgrounds fail the area test
                found.append(r)
        for r in page.cluster_drawings():
            r = fitz.Rect(r)
            if ok(r) and not any(fitz.Rect(b["bbox"]).intersects(r) for b in text_blocks) \
                    and not any(f.intersects(r) for f in found):
                found.append(r)
    except Exception:
        return []
    out = []
    for r in sorted(found, key=lambda r: (r.y0, r.x0)):
        tag = _render_clip_img(page, r, budget)
        if tag:
            out.append((r.y0, r.x0, tag))
    return out


def _extract_pdf_to_html(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    output_parts = []  # str, or list-of-rows for a table (so tables split across pages can merge)
    budget = {"n": 0, "b64": 0}

    for page_idx, page in enumerate(doc):
        page_dict = page.get_text("dict")
        page_width = page.rect.width

        page_parts = []  # (y, x, str | rows)
        tables = []
        try:
            tabs = page.find_tables()
            if tabs and tabs.tables:
                tables = tabs.tables
            else:
                tabs_fb = page.find_tables(vertical_strategy="text", horizontal_strategy="text")
                if tabs_fb and tabs_fb.tables:
                    tables = tabs_fb.tables
        except Exception:
            tables = []

        table_rows = []
        for t in tables:
            try:
                rows = t.extract()
            except Exception:
                rows = None
            # 1-row/1-col "tables" are usually boxed paragraphs, keep them as text
            # unless it's a 1-row 2-column bilingual clause table.
            is_1row_bilingual = (
                rows
                and len(rows) == 1
                and len(rows[0]) == 2
                and bilingual_extractor.detect_bilingual_columns(rows)[0]
            )
            if rows and len(rows[0]) >= 2 and (len(rows) >= 2 or is_1row_bilingual):
                table_rows.append((t.bbox, rows))
        table_bboxes = [tb for tb, _ in table_rows]
        for tb, rows in table_rows:
            page_parts.append((tb[1], tb[0], rows))

        blocks = page_dict.get("blocks", [])
        text_blocks = [b for b in blocks if "lines" in b]
        text_blocks.sort(key=lambda b: (round(b["bbox"][1] / 6) * 6, b["bbox"][0]))

        page_parts.extend(_pdf_page_graphics(page, text_blocks, table_bboxes, budget))

        for b in text_blocks:
            bbox = b["bbox"]

            cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            if any(t[0] - 3 <= cx <= t[2] + 3 and t[1] - 3 <= cy <= t[3] + 3 for t in table_bboxes):
                continue  # text already emitted inside the table

            lines = b.get("lines", [])
            if not lines:
                continue

            block_spans = []
            for l in lines:
                for s in l.get("spans", []):
                    t = s.get("text", "")
                    if t and t.strip():
                        block_spans.append(s)

            if not block_spans:
                continue

            primary_span = block_spans[0]
            font_size = round(primary_span.get("size", 11.0), 1)
            font_name = primary_span.get("font", "Arial")
            flags = primary_span.get("flags", 0)
            is_bold = bool(flags & 2 or "bold" in font_name.lower())
            is_italic = bool(flags & 1 or "italic" in font_name.lower())

            block_x0 = bbox[0]
            block_x1 = bbox[2]
            block_center = (block_x0 + block_x1) / 2
            page_center = page_width / 2
            is_centered = abs(block_center - page_center) < 40 and (block_x1 - block_x0) < (page_width * 0.78)

            line_htmls = []
            for l in lines:
                span_strs = []
                for s in l.get("spans", []):
                    st = s.get("text", "")
                    if not st:
                        continue
                    s_flags = s.get("flags", 0)
                    s_font = s.get("font", "")
                    s_bold = bool(s_flags & 2 or "bold" in s_font.lower())
                    s_italic = bool(s_flags & 1 or "italic" in s_font.lower())
                    escaped = html.escape(st)
                    if s_bold and not is_bold:
                        escaped = f"<strong>{escaped}</strong>"
                    if s_italic and not is_italic:
                        escaped = f"<em>{escaped}</em>"
                    span_strs.append(escaped)
                line_htmls.append(''.join(span_strs))

            full_block_text = ' '.join(line_htmls).strip()
            full_block_text = full_block_text.replace('\u200b', '').strip()
            if not full_block_text:
                continue

            style_attr = []
            if is_centered:
                style_attr.append("text-align: center;")

            tag = "p"
            classes = ["ed-doc-p"]

            if font_size >= 19 or (font_size >= 16 and is_bold and is_centered):
                tag = "h1"
                classes = ["ed-paper-title"]
            elif font_size >= 14 or (
                font_size >= 12
                and is_bold
                and re.match(r'^(pasal|bab|perjanjian|lampiran|skedul)\b', full_block_text, re.I)
            ):
                tag = "h2"
                classes = ["ed-clause-heading"]
            elif font_size >= 13 or (is_bold and re.match(r'^(pasal|bab)\b', full_block_text, re.I)):
                tag = "h3"
                classes = ["ed-clause-subheading"]
            elif re.match(r'^(nomor|no\.)\b', full_block_text, re.I):
                tag = "p"
                classes = ["ed-doc-meta"]
            elif full_block_text.startswith(('●', '•', '- ')):
                tag = "p"
                classes = ["ed-doc-bullet"]

            style_str = f' style="{" ".join(style_attr)}"' if style_attr else ''
            class_str = f' class="{" ".join(classes)}"'

            if is_bold and tag == "p" and "<strong>" not in full_block_text:
                full_block_text = f"<strong>{full_block_text}</strong>"
            if is_italic and tag == "p" and "<em>" not in full_block_text:
                full_block_text = f"<em>{full_block_text}</em>"

            page_parts.append((bbox[1], bbox[0], f'<{tag}{class_str}{style_str}>{full_block_text}</{tag}>'))

        if page_parts:
            page_parts.sort(key=lambda p: (round(p[0] / 6) * 6, p[1]))
            items = [p[2] for p in page_parts]
            prev = output_parts[-1] if output_parts else None
            if isinstance(prev, list) and isinstance(items[0], list) and len(prev[0]) == len(items[0][0]):
                # table continues across the page break: join rows, drop a repeated header
                first = items.pop(0)
                prev.extend(first[1:] if first[0] == prev[0] else first)
            elif output_parts:
                output_parts.append('<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true"></div>')
            output_parts.extend(items)

    output_parts = [_pdf_table_html(p) if isinstance(p, list) else p for p in output_parts]
    full_html = '\n'.join(output_parts)
    plain_text = re.sub(r'<[^>]+>', '', full_html).replace('\u200b', '').strip()
    if len(plain_text) < 40 or _is_text_garbled(plain_text):
        return ""
    return full_html


def _format_docx_runs_html(p) -> str:
    return docx_html.runs_html(p)


def _extract_docx_to_html(docx_path: str) -> str:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(docx_path)
    output_parts = []
    docx_html.reset_budget()

    num_fmt_map = {}
    try:
        np = doc.part.numbering_part.element
        abs_map = {}
        for abs_el in np.xpath('.//w:abstractNum'):
            abs_ids = abs_el.xpath('./@w:abstractNumId')
            if not abs_ids:
                continue
            aid = abs_ids[0]
            for lvl in abs_el.xpath('./w:lvl'):
                ilvls = lvl.xpath('./@w:ilvl')
                fmts = lvl.xpath('./w:numFmt/@w:val')
                if ilvls and fmts:
                    abs_map[(aid, ilvls[0])] = fmts[0]
        for num_el in np.xpath('.//w:num'):
            nids = num_el.xpath('./@w:numId')
            aids = num_el.xpath('./w:abstractNumId/@w:val')
            if nids and aids:
                nid = nids[0]
                aid = aids[0]
                for (a_key, ilvl_key), fmt_val in abs_map.items():
                    if a_key == aid:
                        num_fmt_map[(nid, ilvl_key)] = fmt_val
    except Exception:
        pass

    num_counters: dict[tuple[str, str], int] = {}

    def _flatten(el):
        # unwrap content controls (w:sdt) so tables/paragraphs inside them are not lost
        for ch in el:
            tn = ch.tag.split('}')[-1]
            if tn == 'sdt':
                content = ch.find('{*}sdtContent')
                if content is not None:
                    yield from _flatten(content)
            elif tn in ('p', 'tbl'):
                yield ch

    body_elements = list(_flatten(doc.element.body))
    total_elements = len(body_elements)
    has_explicit_breaks = False
    non_empty_p_index = 0
    empty_run = 0

    for idx, child in enumerate(body_elements):
        tag_name = child.tag.split('}')[-1]
        if tag_name == 'p':
            p = Paragraph(child, doc)
            t = p.text.strip()

            # Detect explicit section / page breaks from DOCX (including empty break paragraphs)
            has_sect = bool(child.xpath('.//w:sectPr'))
            has_br_page = bool(child.xpath('.//w:br[@w:type="page"]'))
            has_last_br = bool(child.xpath('.//w:lastRenderedPageBreak'))
            page_break_before = bool(p.paragraph_format.page_break_before)
            is_trailing = (idx == total_elements - 1)

            if (has_sect or has_br_page or has_last_br or page_break_before) and not is_trailing and output_parts:
                output_parts.append('<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true"></div>')
                has_explicit_breaks = True

            tb_lines = docx_html.textbox_lines(child)
            pcss = docx_html.para_css(p)
            if not t:
                # picture-only paragraph (signature scan, stamp, logo) or text-box-only paragraph
                pic_html = docx_html.images_html(child, p.part)
                if pic_html:
                    empty_run = 0
                    output_parts.append(f'<p class="ed-doc-p"{docx_html.style_attr(pcss, {"text-align": "left"})}>{pic_html}</p>')
                elif output_parts and not is_trailing and not tb_lines:
                    # blank paragraph = deliberate vertical spacing in the original
                    empty_run += 1
                    if empty_run <= 3:
                        output_parts.append(f'<p class="ed-doc-p"{docx_html.style_attr(pcss, {"text-align": "left"})}><br/></p>')
                for tl in tb_lines:
                    output_parts.append(f'<p class="ed-doc-p">{html.escape(tl)}</p>')
                continue

            empty_run = 0
            non_empty_p_index += 1
            line_html = docx_html.runs_html(p)
            align = pcss.get("text-align")
            align_style = docx_html.style_attr(pcss)

            style_name = (p.style.name or '').lower() if p.style else ''
            num_pr = p._p.xpath('.//w:numPr')
            all_runs_bold = bool(p.runs) and all((r.bold or not r.text.strip()) for r in p.runs if r.text)
            is_all_caps_heading = (
                len(t) <= 110
                and t == t.upper()
                and any(c.isalpha() for c in t)
                and not t.startswith(('•', '●', '-'))
                and (all_runs_bold or re.match(r'^(PASAL|BAB|LAMPIRAN|BAGIAN)\b', t))
            )

            if 'heading 1' in style_name or 'title' in style_name:
                output_parts.append(f'<h1 class="ed-paper-title"{docx_html.style_attr(pcss, {"text-align": "left"})}>{line_html}</h1>')
            elif non_empty_p_index == 1 and (align == 'center' or all_runs_bold) and len(t) <= 140:
                output_parts.append(f'<h1 class="ed-paper-title"{docx_html.style_attr(pcss, {"text-align": "left"})}>{line_html}</h1>')
            elif 'heading 4' in style_name or (non_empty_p_index <= 5 and re.match(r'^(nomor|no\.?)\b', t, re.I)):
                output_parts.append(f'<p class="ed-doc-meta"{docx_html.style_attr(pcss, {"text-align": "center"})}>{line_html}</p>')
            elif 'heading 2' in style_name or 'subtitle' in style_name:
                output_parts.append(f'<h2 class="ed-clause-heading"{align_style}>{line_html}</h2>')
            elif 'heading 3' in style_name or re.match(r'^(pasal|bab)\b', t, re.I) or is_all_caps_heading:
                # alignment comes from the document itself (direct or style); never invented
                output_parts.append(f'<h2 class="ed-clause-heading"{docx_html.style_attr(pcss, {"text-align": "left"})}>{line_html}</h2>')
            elif num_pr:
                nids = p._p.xpath('.//w:numPr/w:numId/@w:val')
                ilvls = p._p.xpath('.//w:numPr/w:ilvl/@w:val')
                nid = nids[0] if nids else '1'
                ilvl = ilvls[0] if ilvls else '0'
                fmt = num_fmt_map.get((nid, ilvl), 'decimal')
                if fmt == 'bullet':
                    output_parts.append(f'<p class="ed-doc-bullet"{align_style}>• {line_html}</p>')
                else:
                    key = (nid, ilvl)
                    num_counters[key] = num_counters.get(key, 0) + 1
                    c_idx = num_counters[key]
                    output_parts.append(f'<p class="ed-doc-bullet"{align_style}><strong>{c_idx}.</strong> {line_html}</p>')
            elif t.startswith(('●', '•', '- ')):
                output_parts.append(f'<p class="ed-doc-bullet"{align_style}>{line_html}</p>')
            else:
                output_parts.append(f'<p class="ed-doc-p"{docx_html.style_attr(pcss, {"text-align": "left"})}>{line_html}</p>')

            for tl in tb_lines:
                output_parts.append(f'<p class="ed-doc-p">{html.escape(tl)}</p>')

        elif tag_name == 'tbl':
            tbl_obj = Table(child, doc)
            raw_rows = [[c.text or "" for c in row.cells] for row in tbl_obj.rows]
            is_bi, col_order = bilingual_extractor.detect_bilingual_columns(raw_rows)
            is_borderless = bilingual_extractor.is_docx_table_borderless(tbl_obj)
            tbl_out = docx_html.table_to_html(
                tbl_obj,
                bilingual_order=col_order if (is_bi and col_order) else None,
                borderless=is_borderless,
                page_char_budget=(4500 if (is_bi and col_order) else 2500) if not has_explicit_breaks else 0,
            )
            if tbl_out and 'data-page-break="true"' in tbl_out:
                has_explicit_breaks = True
            if tbl_out:
                empty_run = 0
                output_parts.append(tbl_out)

    # If the DOCX has no explicit XML page breaks and is multi-page length (>= 2400 chars),
    # insert logical page breaks before major headings/tables every ~2600 characters.
    if not has_explicit_breaks and output_parts:
        paginated_parts = []
        running_chars = 0
        for i, part in enumerate(output_parts):
            part_plain = re.sub(r'<[^>]+>', '', part).strip()
            is_break_candidate = part.startswith('<h2 ') or part.startswith('<table ')
            if running_chars >= 2500 and is_break_candidate and i < len(output_parts) - 1:
                paginated_parts.append(
                    '<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true" data-auto-break="true"></div>'
                )
                running_chars = 0
            paginated_parts.append(part)
            running_chars += len(part_plain)
        output_parts = paginated_parts

    full_html = '\n'.join(output_parts)
    plain_text = re.sub(r'<[^>]+>', '', full_html).replace('\u200b', '').strip()
    if len(plain_text) < 20:
        return ""
    return full_html


def _vlm_inline(s: str) -> str:
    """Escape VLM text and turn markdown **bold** / *italic* into HTML; drop stray markers."""
    s = html.escape(s.strip())
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'<em>\1</em>', s)
    return s.replace('**', '').strip()


_VLM_PREAMBLE_RE = re.compile(
    r'^(?:berikut\s+(?:adalah|ini)|di\s+bawah\s+ini|inilah|hasil\s+ekstraksi|ekstraksi\s+teks|'
    r'here\s+is|here\'s|this\s+is\s+the|below\s+is)\b[^\n]{0,220}$',
    re.I,
)


def _clean_vlm_text(text: str) -> str:
    """Strip VLM chatter that is not document content: preambles ('Berikut adalah ekstraksi...'),
    code fences, orphan markdown markers and horizontal rules."""
    out = []
    for raw in (text or '').replace('\r\n', '\n').split('\n'):
        s = raw.strip()
        if s.startswith('```'):
            continue
        if _VLM_PREAMBLE_RE.match(s) and re.search(r'ekstrak|teks|dokumen|extract|text|document', s, re.I):
            continue
        if re.fullmatch(r'\*{1,3}|_{3,}|-{3,}|={3,}', s):
            continue
        out.append(raw)
    return '\n'.join(out)


def _split_md_row(line: str) -> list:
    raw = line.strip()
    if raw.startswith('|'):
        raw = raw[1:]
    if raw.endswith('|'):
        raw = raw[:-1]
    return [c.strip() for c in raw.split('|')]


def _parse_markdown_table_to_html(table_lines: list) -> str:
    """Converts markdown table lines into a clean HTML table.

    Tolerates what VLMs actually emit: ragged rows (padded / colspan'd), repeated separator
    rows, single-cell 'section title' rows inside a wider table, and tables split by page wraps.
    """
    rows = []
    for line in table_lines:
        cells = _split_md_row(line)
        if cells and all(re.match(r'^:?-{2,}:?$', c) or not c for c in cells) and any(cells):
            rows.append(None)  # separator marker
            continue
        rows.append(cells)

    data = [r for r in rows if r is not None]
    if not data:
        return ""
    ncols = max(len(r) for r in data)

    # A header exists when the first row is directly followed by a separator marker.
    has_header = len(rows) > 1 and rows[0] is not None and rows[1] is None

    def cell_html(c):
        return _vlm_inline(c).replace('&lt;br&gt;', '<br/>').replace('&lt;br/&gt;', '<br/>')

    def tr(r, tag):
        if len(r) == 1 and ncols > 1:  # section-title row spanning the table
            return f'<tr><th colspan="{ncols}">{cell_html(r[0])}</th></tr>'
        cells = list(r) + [''] * (ncols - len(r))
        return '<tr>' + ''.join(f'<{tag}>{cell_html(c)}</{tag}>' for c in cells) + '</tr>'

    parts = ['<table class="ed-doc-table">']
    body = data
    if has_header:
        parts.append('<thead>' + tr(data[0], 'th') + '</thead>')
        body = data[1:]
    if body:
        parts.append('<tbody>' + ''.join(tr(r, 'td') for r in body) + '</tbody>')
    parts.append('</table>')
    return ''.join(parts)


def _convert_vlm_text_to_html(text: str, stored_filename: str = "", pdf_path: str = "") -> str:
    PAGE_BREAK_HTML = '<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true"></div>'
    lines = _clean_vlm_text(text).splitlines()
    output_parts = []
    first_heading = True
    table_buffer = []
    cur_page = None
    sig_pages = set()
    img_budget = {"n": 0, "b64": 0}
    pdf_doc = None

    def _signature_crop():
        # shortcut: VLM text has no coordinates, so crop the lower 40% of the page that holds
        # the signature tag; upgrade by asking the VLM for a bbox.
        nonlocal pdf_doc
        if not pdf_path or cur_page is None or cur_page in sig_pages:
            return
        sig_pages.add(cur_page)
        try:
            pdf_doc = pdf_doc or fitz.open(pdf_path)
            pg = pdf_doc[cur_page]
            r = pg.rect
            tag = _render_clip_img(pg, fitz.Rect(r.x0, r.y0 + r.height * 0.6, r.x1, r.y1), img_budget)
            if tag:
                output_parts.append(tag)
        except Exception:
            pass

    def _flush_table():
        nonlocal table_buffer
        if table_buffer:
            tbl_html = _parse_markdown_table_to_html(table_buffer)
            if tbl_html:
                output_parts.append(tbl_html)
            table_buffer = []

    def _next_nonblank(i):
        for j in range(i + 1, len(lines)):
            if lines[j].strip():
                return lines[j].strip()
        return ""

    for line_idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            # a blank line inside a table (VLM page wrap) must not split it in two
            if not (table_buffer and _next_nonblank(line_idx).startswith('|')):
                _flush_table()
            continue

        # Signature & Paraf Badges
        if re.match(r'^(?:\[(?:paraf|tanda tangan)\b.*\]|(?:paraf|tanda tangan)\s*:)', line, re.I):
            _flush_table()
            output_parts.append(
                f'<div class="ed-doc-signature-badge" data-signature="true">{_vlm_inline(line)}</div>'
            )
            if re.match(r'^\[?tanda tangan', line, re.I):
                _signature_crop()
            continue

        # Handle markdown table rows
        if line.startswith('|') or ('|' in line and not line.startswith(('###', '##', '#', '**', '---', '[')) and not re.match(r'^(?:pasal|bab|lampiran|skedul|jadwal|nomor|no\.)\b', line, re.I)):
            table_buffer.append(line)
            continue
        else:
            _flush_table()

        if re.match(r'^---+\s*(?:halaman|page)\s*\d+\s*---+', line, re.I):
            cur_page = int(re.search(r'\d+', line).group()) - 1
            if output_parts:
                output_parts.append(PAGE_BREAK_HTML)
            continue

        # Markdown headings that are not workflow headers
        md_head = re.match(r'^#{1,6}\s+(.*)$', line)
        if md_head and not re.match(r'^\[?diagram alir', md_head.group(1), re.I):
            line = md_head.group(1).strip()
            if not line:
                continue
            output_parts.append(f'<h2 class="ed-clause-heading">{_vlm_inline(line)}</h2>')
            continue

        # Clause headings: short standalone lines only. A long line or one ending in
        # sentence punctuation is body text that merely starts with "Pasal 3 ...".
        if (
            re.match(r'^(?:\*\*)?(?:pasal|bab|lampiran|skedul|jadwal|ketentuan\s+(?:tambahan|penutup|lain))\b', line, re.I)
            and len(line) <= 90
            and not line.rstrip('*').endswith(('.', ';', ','))
        ):
            output_parts.append(f'<h2 class="ed-clause-heading">{_vlm_inline(line)}</h2>')
            continue

        # Flowchart / Procedural Swimlane Headers
        if re.match(r'^(?:###\s*\[?diagram alir|prosedur eskalasi|escalation procedure|\*\*diagram alir|\*\*production problem reporting)\b', line, re.I):
            clean_title = re.sub(r'^[#\s\[\]\*]+', '', line).rstrip(']*').strip()
            output_parts.append(f'<div class="ed-doc-workflow-header"><strong>{html.escape(clean_title)}</strong></div>')
            continue

        if re.match(r'^\*\*.*(?:swimlane|pihak terkait).*\*\*[:\s]?', line, re.I):
            output_parts.append(f'<div class="ed-doc-workflow-swimlane">{_vlm_inline(line)}</div>')
            continue

        if re.match(r'^(?:nomor|no\.)\b', line, re.I):
            output_parts.append(f'<p class="ed-doc-meta" style="text-align: center;">{_vlm_inline(line)}</p>')
            continue

        if first_heading and len(line) <= 120 and not line.endswith(('.', ':')) and not re.match(r'^(?:nomor|no\.)\b', line, re.I):
            output_parts.append(f'<h1 class="ed-paper-title" style="text-align: center;">{_vlm_inline(line)}</h1>')
            first_heading = False
            continue

        if line.startswith(('●', '•', '- ', '* ')):
            clean_item = re.sub(r'^[●•\-\*]\s*', '', line)
            output_parts.append(f'<p class="ed-doc-bullet">{_vlm_inline(clean_item)}</p>')
            continue

        output_parts.append(f'<p class="ed-doc-p">{_vlm_inline(line)}</p>')

    _flush_table()
    return "\n".join(output_parts)


def _reconstruct_paginated_document_html(summary: dict, clauses: list, stored_filename: str = "") -> str:
    """Builds a beautifully paginated, authentic legal document from structured clauses.
    Places seamless page breaks cleanly between clause boundaries (never orphaning headings).
    """
    PAGE_BREAK_HTML = '<div class="ed-page-break" contenteditable="false" role="separator" aria-label="Page Break" data-page-break="true"></div>'
    TARGET_PAGE_CHARS = 2100  # Natural A4 legal sheet capacity (~350 words at 12pt, 1.7 line height)
    MIN_PAGE_CHARS = 1200     # Minimum chars before considering a page break to prevent sparse sheets

    raw_title = summary.get("jenis_dokumen") or ""
    if not raw_title or len(raw_title) <= 5:
        if stored_filename:
            raw_title = stored_filename.rsplit(".", 1)[0].replace("_", " ")
        else:
            raw_title = "Perjanjian Kerjasama"
    doc_title = raw_title
    pokok = summary.get("pokok_perjanjian") or ""
    p1 = summary.get("pihak_pertama")
    p2 = summary.get("pihak_kedua")

    output_parts = [f'<h1 class="ed-paper-title">{html.escape(doc_title)}</h1>']
    if pokok and pokok.lower() not in ["tidak teridentifikasi", "-"]:
        output_parts.append(f'<p class="ed-clause-subheading" style="text-align: center;"><strong>{html.escape(pokok)}</strong></p>')
    if p1 or p2:
        output_parts.append(f'<p class="ed-doc-p" style="text-align: center; margin-bottom: 24px;">Antara <strong>{html.escape(p1 or "-")}</strong> dan <strong>{html.escape(p2 or "-")}</strong></p>')

    current_page_chars = sum(len(re.sub(r'<[^>]+>', '', p)) for p in output_parts)

    # The compliance analyzer emits clauses in scoring order, not document order.
    # Sort by the article number parsed from the pasal/judul label so pasal 10
    # precedes pasal 13; clauses without a parseable number sort last (stable).
    def _clause_order(clause: dict):
        label = (clause.get("pasal") or clause.get("judul") or "")
        m = re.search(r"\d+", label)
        return (0, int(m.group())) if m else (1, 0)

    for clause in sorted(clauses, key=_clause_order):
        pasal = (clause.get("pasal") or clause.get("judul") or "").strip()
        isi = (clause.get("isi_pasal") or clause.get("deskripsi") or clause.get("teks") or "").strip()
        clause_chars = len(pasal) + len(isi)

        # Seamless page boundary: only break cleanly BEFORE a clause heading to prevent orphaned headings
        if (current_page_chars + clause_chars > TARGET_PAGE_CHARS) and (current_page_chars >= MIN_PAGE_CHARS):
            output_parts.append(PAGE_BREAK_HTML)
            current_page_chars = 0

        clause_parts = []
        if pasal:
            clause_parts.append(f'<h2 class="ed-clause-heading">{html.escape(pasal)}</h2>')

        # Clause bodies often begin with their own heading line ("PASAL 10", "SANKSI"),
        # which would duplicate the synthesized heading above in a different casing.
        # Drop leading body lines that restate the pasal heading.
        isi_lines = isi.splitlines() if isi else []
        if pasal and isi_lines:
            pasal_key = re.sub(r"\W", "", pasal).lower()
            pasal_num = re.search(r"\d+", pasal)
            while isi_lines:
                first = isi_lines[0].strip()
                first_key = re.sub(r"\W", "", first).lower()
                is_dup = bool(first_key) and (
                    first_key == pasal_key
                    or (pasal_num is not None
                        and re.match(r"^(PASAL|BAB)\s+" + re.escape(pasal_num.group()) + r"\b", first, re.IGNORECASE))
                )
                if is_dup:
                    isi_lines.pop(0)
                else:
                    break

        if isi_lines:
            for line in isi_lines:
                lt = line.strip()
                if lt:
                    # For long single clauses (>2400 chars), break cleanly at paragraph boundaries
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


def _extract_document_html(report_id: str, stored_filename: str) -> str:
    path = _find_contract_file(report_id, stored_filename)
    if path and os.path.exists(path):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            try:
                extracted = _extract_pdf_to_html(path)
                plain = re.sub(r'<[^>]+>', '', extracted).replace('\u200b', '').strip()
                if len(plain) >= 40 and not _is_text_garbled(plain):
                    return extracted
                print(f"[Editor] PDF text layer in {path} is garbled or empty ({len(plain)} chars). Falling back to authentic structured reconstruction...")
            except Exception as e:
                print(f"[Editor] PDF extraction failed for {path}: {e}")
        elif ext in [".docx", ".doc"]:
            try:
                extracted = _extract_docx_to_html(path)
                plain = re.sub(r'<[^>]+>', '', extracted).replace('\u200b', '').strip()
                if len(plain) >= 20:
                    return _sanitize_mojibake(extracted)
                print(f"[Editor] DOCX XML text layer in {path} is empty ({len(plain)} chars). Checking embedded scanned images...")
            except Exception as e:
                print(f"[Editor] DOCX extraction failed for {path}: {e}")
        elif ext == ".txt":
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                parts = []
                for line in lines:
                    t = line.strip()
                    if t:
                        parts.append(f'<p class="ed-doc-p">{html.escape(t)}</p>')
                if parts:
                    return "\n".join(parts)
            except Exception as e:
                print(f"[Editor] TXT extraction failed for {path}: {e}")

    # Primary Fallback for scanned/garbled PDFs: Run full-fidelity VLM extraction on the physical PDF
    if path and os.path.exists(path) and ext == ".pdf":
        try:
            from services.rag_service import extract_text_hybrid
            print(f"[Editor] Triggering full-document VLM vision extraction for {path}...")
            vlm_text = extract_text_hybrid(path, force_vlm=True)
            if vlm_text and len(vlm_text.strip()) >= 40 and not _is_text_garbled(vlm_text):
                vlm_html = _convert_vlm_text_to_html(vlm_text, stored_filename, path)
                if vlm_html:
                    return _sanitize_mojibake(vlm_html)
        except Exception as e:
            print(f"[Editor] VLM vision extraction failed for {path}: {e}")

    # Secondary Fallback: Compare compliance_history reconstruction vs full document chunks
    reconstructed_candidate = ""
    plain_rec_len = 0
    try:
        hist_row = pg_service.query_one(
            "SELECT results_json, filename FROM compliance_history WHERE id = %s",
            (report_id,),
        )
        if hist_row and hist_row.get("results_json"):
            raw_json = hist_row["results_json"]
            data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            clauses = []
            summary = {}
            if isinstance(data, dict):
                clauses = data.get("results") or data.get("report") or []
                summary = data.get("summary") or {}
            elif isinstance(data, list):
                clauses = data

            if clauses:
                reconstructed = _reconstruct_paginated_document_html(
                    summary,
                    clauses,
                    stored_filename or hist_row.get("filename") or "",
                )
                plain_rec = html.unescape(re.sub(r'<[^>]+>', ' ', reconstructed)).replace('\u200b', '').strip()
                if len(plain_rec) >= 40 and not _is_text_garbled(plain_rec):
                    reconstructed_candidate = _sanitize_mojibake(reconstructed)
                    plain_rec_len = len(plain_rec)
    except Exception as e:
        print(f"[Editor] Fallback to compliance_history reconstruction failed: {e}")

    # Fallback 2: Check full document text in chunks table (ordered by id)
    # Prefer chunks if compliance_history is missing or significantly shorter than chunks
    try:
        chunks = pg_service.query(
            "SELECT text FROM chunks WHERE doc_id = %s ORDER BY id ASC",
            (report_id,),
        )
        if chunks:
            cleaned_chunk_parts = []
            last_tag = None
            for c in chunks:
                raw_chunk = (c.get("text") or "").strip()
                if not raw_chunk or _is_text_garbled(raw_chunk):
                    continue
                m_tag = re.match(r"^\[([^\]]+)\]\s*(.*)$", raw_chunk, re.S)
                if m_tag:
                    tag_label = m_tag.group(1).strip()
                    body_text = m_tag.group(2).strip()
                    if tag_label == last_tag and re.match(r"^(pasal|bab|lampiran)\b", body_text, re.I):
                        # Strip repeated heading line on continuation chunk
                        first_nl = body_text.find("\n")
                        if first_nl != -1:
                            body_text = body_text[first_nl + 1:].strip()
                    last_tag = tag_label
                    cleaned_chunk_parts.append(body_text)
                else:
                    cleaned_chunk_parts.append(raw_chunk)

            if cleaned_chunk_parts:
                joined_chunks = "\n\n".join(cleaned_chunk_parts)
                if len(joined_chunks) > plain_rec_len * 1.25:
                    vlm_html = _convert_vlm_text_to_html(joined_chunks, stored_filename, path if (path and ext == ".pdf") else "")
                    if vlm_html:
                        return _sanitize_mojibake(vlm_html)
    except Exception as e:
        print(f"[Editor] Fallback chunks query failed: {e}")

    if reconstructed_candidate and (ext not in [".docx", ".doc"] or plain_rec_len >= 4000):
        return reconstructed_candidate

    # Fallback 3: Image-based DOCX where XML text is empty and DB records are incomplete
    if path and os.path.exists(path) and ext in [".docx", ".doc"]:
        try:
            from routers.compliance import extract_docx_content
            print(f"[Editor] Triggering VLM vision extraction for image-based DOCX {path}...")
            vlm_text = extract_docx_content(path, stored_filename or os.path.basename(path), use_ocr=True)
            if vlm_text and len(vlm_text.strip()) >= 40 and not _is_text_garbled(vlm_text):
                vlm_html = _convert_vlm_text_to_html(vlm_text, stored_filename)
                if vlm_html:
                    return _sanitize_mojibake(vlm_html)
        except Exception as e:
            print(f"[Editor] DOCX VLM vision extraction failed for {path}: {e}")

    if reconstructed_candidate:
        return reconstructed_candidate

    # Clean fallback: document title with clean editor placeholder
    doc_title = os.path.splitext(stored_filename)[0] if stored_filename else "Dokumen"
    return (
        f'<h1 class="ed-paper-title">{html.escape(doc_title)}</h1>\n'
        f'<p class="ed-doc-p">Dokumen siap untuk ditinjau dan diedit.</p>'
    )


@router.get("/contracts/{report_id}/editor")
def get_editor_draft(
    report_id: str,
    force_reextract: bool = False,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])
    draft = None
    if not force_reextract:
        draft = pg_service.query_one(
            "SELECT content_html, content_json, version, "
            "to_char(updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS updated_at "
            "FROM document_editor_drafts WHERE report_id = %s",
            (report_id,),
        )
    draft_html = (draft.get("content_html") or "") if draft else ""
    draft_plain = html.unescape(re.sub(r'<[^>]+>', ' ', draft_html)).replace('\u200b', '').strip()
    is_blank = len(draft_plain) < 40
    is_old_dummy = bool(
        draft
        and (
            is_blank
            or "ed-parties-block" in draft_html
            or "Perjanjian ini dibuat dan disepakati oleh dan antara" in draft_html
            or "Perjanjian ini mengatur kerjasama penyediaan layanan integrasi sistem" in draft_html
            or '("First party")' in draft_html
            or '("Second party")' in draft_html
            or '(&quot;First party&quot;)' in draft_html
            or '(&quot;Second party&quot;)' in draft_html
            or '<h1 class="ed-paper-title" style="text-align: center;">PKS</h1>' in draft_html
            or '<h1 class="ed-paper-title">PKS</h1>' in draft_html
        )
    )

    final_html = draft_html
    final_version = draft.get("version", 1) if draft else 1
    final_updated_at = draft.get("updated_at") if draft else None
    parsed_json = None

    if not draft or not draft.get("content_html") or is_old_dummy or force_reextract:
        # Extract authentic document layout from source uploaded file or compliance clauses
        extracted_html = _extract_document_html(report_id, access["filename"])
        draft_id = f"draft-{uuid.uuid4().hex[:12]}"
        inserted_rows = pg_service.execute_returning(
            """
            INSERT INTO document_editor_drafts (id, report_id, user_id, content_html, version, updated_at)
            VALUES (%s, %s, %s, %s, 1, now())
            ON CONFLICT (report_id) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                content_html = EXCLUDED.content_html,
                version = 1,
                updated_at = now()
            RETURNING to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS updated_at;
            """,
            (draft_id, report_id, current_user["id"], extracted_html),
        )
        final_html = extracted_html
        final_version = 1
        final_updated_at = inserted_rows[0]["updated_at"] if inserted_rows else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    elif draft.get("content_json"):
        try:
            parsed_json = json.loads(draft["content_json"])
        except Exception:
            parsed_json = None

    # Ensure baseline v1 exists in document_editor_versions
    first_ver = pg_service.query_one(
        "SELECT id FROM document_editor_versions WHERE report_id = %s LIMIT 1",
        (report_id,),
    )
    if not first_ver and final_html:
        ver_id = f"ver-{uuid.uuid4().hex[:12]}"
        pg_service.execute(
            """
            INSERT INTO document_editor_versions
                (id, report_id, version_number, title, snapshot_html, created_by, author_name, color_index, changes_json)
            VALUES (%s, %s, 1, 'Versi Awal (Ekstraksi Dokumen)', %s, %s, 'Sistem', 0, '[]')
            ON CONFLICT DO NOTHING
            """,
            (ver_id, report_id, final_html, access["owner_id"]),
        )

    ch_meta = pg_service.query_one(
        "SELECT results_json FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    ch_results = {}
    if ch_meta and ch_meta.get("results_json"):
        try:
            ch_results = json.loads(ch_meta["results_json"])
        except Exception:
            ch_results = {}

    is_bilingual = "bilingual-contract-table" in final_html or ch_results.get("layout_mode") == "bilingual_parallel"
    governing_language = ch_results.get("governing_language") or ("id" if is_bilingual else None)
    discrepancies = ch_results.get("translation_discrepancies") or []
    detected_contract_type = ch_results.get("detected_contract_type") or (ch_results.get("summary") or {}).get("detected_contract_type") or "GENERAL"
    missing_clauses = ch_results.get("missing_clauses") or []

    return {
        "report_id": report_id,
        "filename": access["filename"],
        "content_html": _sanitize_mojibake(final_html),
        "content_json": parsed_json,
        "version": final_version,
        "updated_at": final_updated_at,
        "is_owner": access["is_owner"],
        "my_color_index": access["color_index"],
        "is_extracted": bool(not draft or is_old_dummy or force_reextract),
        "is_bilingual": is_bilingual,
        "governing_language": governing_language,
        "translation_discrepancies": discrepancies,
        "detected_contract_type": detected_contract_type,
        "missing_clauses": missing_clauses,
    }


@router.post("/contracts/{report_id}/editor/re-extract")
def re_extract_editor_source(
    report_id: str,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])
    if not access["is_owner"]:
        raise HTTPException(status_code=403, detail="Hanya pemilik workspace yang dapat mengekstraksi ulang dokumen")

    extracted_html = _extract_document_html(report_id, access["filename"])
    now_iso = datetime.now(timezone.utc).isoformat()
    draft_id = f"draft-{uuid.uuid4().hex[:12]}"
    pg_service.execute(
        """
        INSERT INTO document_editor_drafts (id, report_id, user_id, content_html, version, updated_at)
        VALUES (%s, %s, %s, %s, 1, now())
        ON CONFLICT (report_id) DO UPDATE SET
            content_html = EXCLUDED.content_html,
            version = document_editor_drafts.version + 1,
            updated_at = now();
        """,
        (draft_id, report_id, current_user["id"], extracted_html),
    )

    updated_draft = pg_service.query_one(
        "SELECT version, to_char(updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS updated_at "
        "FROM document_editor_drafts WHERE report_id = %s",
        (report_id,),
    )
    return {
        "status": "success",
        "report_id": report_id,
        "filename": access["filename"],
        "content_html": extracted_html,
        "version": updated_draft["version"] if updated_draft else 1,
        "updated_at": updated_draft["updated_at"] if updated_draft else now_iso,
        "is_bilingual": "bilingual-contract-table" in extracted_html,
    }


@router.put("/contracts/{report_id}/editor")
def save_editor_draft(
    report_id: str,
    req: DraftSaveRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])
    draft_id = str(uuid.uuid4())
    json_str = json.dumps(req.content_json) if req.content_json is not None else None
    rows = pg_service.execute_returning(
        """
        INSERT INTO document_editor_drafts (id, report_id, user_id, content_html, content_json, version, updated_at)
        VALUES (%s, %s, %s, %s, %s, 1, now())
        ON CONFLICT (report_id) DO UPDATE
        SET user_id = EXCLUDED.user_id,
            content_html = EXCLUDED.content_html,
            content_json = EXCLUDED.content_json,
            version = document_editor_drafts.version + 1,
            updated_at = now()
        RETURNING version, to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS updated_at
        """,
        (draft_id, report_id, current_user["id"], req.content_html, json_str),
    )
    doc_notifications.notify_collaborators(report_id, current_user["id"], event_type="document_edited")
    saved = rows[0] if rows else {"version": 1, "updated_at": None}
    return {
        "status": "saved",
        "version": saved["version"],
        "updated_at": saved["updated_at"],
    }


@router.get("/contracts/{report_id}/editor/comments")
def list_editor_comments(report_id: str, current_user: dict = Depends(get_current_user)):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])
    rows = pg_service.query(
        """
        SELECT id, report_id, author_name, selected_text, comment_text, replies_json, resolved,
               to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at,
               to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at
        FROM document_editor_comments
        WHERE report_id = %s
        ORDER BY created_at ASC
        """,
        (report_id,),
    )
    return {"comments": [_serialize_comment(r) for r in rows]}


@router.post("/contracts/{report_id}/editor/comments")
def create_editor_comment(
    report_id: str,
    req: CommentCreateRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])
    comment_id = req.id or f"cmt-{uuid.uuid4().hex[:10]}"
    author = current_user.get("username") or "Reviewer"
    rows = pg_service.execute_returning(
        """
        INSERT INTO document_editor_comments
            (id, report_id, user_id, author_name, selected_text, comment_text, replies_json, resolved)
        VALUES (%s, %s, %s, %s, %s, %s, '[]', false)
        RETURNING id, report_id, author_name, selected_text, comment_text, replies_json, resolved,
                  to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at,
                  to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at
        """,
        (comment_id, report_id, current_user["id"], author, req.selected_text, req.comment_text),
    )
    doc_notifications.notify_collaborators(
        report_id,
        current_user["id"],
        event_type="comment_added",
        reference_id=comment_id,
    )
    return {"comment": _serialize_comment(rows[0])}


@router.post("/contracts/{report_id}/editor/comments/{comment_id}/reply")
def reply_editor_comment(
    report_id: str,
    comment_id: str,
    req: CommentReplyRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])
    existing = pg_service.query_one(
        "SELECT user_id, replies_json FROM document_editor_comments WHERE id = %s AND report_id = %s",
        (comment_id, report_id),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Comment not found")

    try:
        replies = json.loads(existing.get("replies_json") or "[]")
    except Exception:
        replies = []

    replies.append(
        {
            "id": f"rpl-{uuid.uuid4().hex[:8]}",
            "user_id": current_user["id"],
            "author_name": current_user.get("username") or "Reviewer",
            "text": req.reply_text,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )

    rows = pg_service.execute_returning(
        """
        UPDATE document_editor_comments
        SET replies_json = %s, updated_at = now()
        WHERE id = %s AND report_id = %s
        RETURNING id, report_id, author_name, selected_text, comment_text, replies_json, resolved,
                  to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at,
                  to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at
        """,
        (json.dumps(replies), comment_id, report_id),
    )

    participant_ids = [existing.get("user_id")] + [
        r.get("user_id") for r in replies if isinstance(r, dict) and r.get("user_id")
    ]
    doc_notifications.notify_collaborators(
        report_id,
        current_user["id"],
        event_type="comment_reply",
        target_user_ids=participant_ids,
        reference_id=comment_id,
    )
    return {"comment": _serialize_comment(rows[0])}


@router.patch("/contracts/{report_id}/editor/comments/{comment_id}/resolve")
def resolve_editor_comment(
    report_id: str,
    comment_id: str,
    req: CommentResolveRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])
    rows = pg_service.execute_returning(
        """
        UPDATE document_editor_comments
        SET resolved = %s, updated_at = now()
        WHERE id = %s AND report_id = %s
        RETURNING id, report_id, user_id, author_name, selected_text, comment_text, replies_json, resolved,
                  to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at,
                  to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS updated_at
        """,
        (req.resolved, comment_id, report_id),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Comment not found")
    if rows[0].get("user_id"):
        doc_notifications.notify_collaborators(
            report_id,
            current_user["id"],
            event_type="comment_resolved",
            target_user_ids=[rows[0]["user_id"]],
            reference_id=comment_id,
        )
    return {"comment": _serialize_comment(rows[0])}


@router.delete("/contracts/{report_id}/editor/comments/{comment_id}")
def delete_editor_comment(
    report_id: str,
    comment_id: str,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])
    existing = pg_service.query_one(
        "SELECT user_id FROM document_editor_comments WHERE id = %s AND report_id = %s",
        (comment_id, report_id),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not access["is_owner"] and existing["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Tidak dapat menghapus komentar pengguna lain")

    pg_service.execute(
        "DELETE FROM document_editor_comments WHERE id = %s AND report_id = %s",
        (comment_id, report_id),
    )
    return {"status": "deleted", "id": comment_id}


@router.post("/contracts/{report_id}/editor/export/docx")
def export_editor_docx(
    report_id: str,
    req: ExportDocxRequest,
    current_user: dict = Depends(get_current_user),
):
    import base64
    import io
    import os
    import re
    import docx

    report = verify_report_access(report_id, current_user["id"])
    from services.html_docx import build_docx
    doc = build_docx(req.content_html, bilingual_view=req.bilingual_view or "both")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    base_name = os.path.splitext(report["filename"])[0]
    return {
        "filename": f"{base_name}_edited.docx",
        "data": base64.b64encode(buf.read()).decode("utf-8"),
    }


@router.post("/contracts/{report_id}/editor/ai-assist")
def ai_assist_clause(
    report_id: str,
    req: AiAssistRequest,
    current_user: dict = Depends(get_current_user),
):
    verify_report_access(report_id, current_user["id"])

    if (req.mode or "").strip().lower() == "draft_missing_clause":
        row = pg_service.query_one(
            "SELECT results_json FROM compliance_history WHERE id = %s",
            (report_id,),
        )
        summary = {}
        if row and row.get("results_json"):
            try:
                summary = (json.loads(row["results_json"]) or {}).get("summary") or {}
            except Exception:
                summary = {}
        pihak_kita = summary.get("pihak_kita") or summary.get("pihak_pertama") or "Pihak Pertama"
        peran_kita = summary.get("peran_kita") or ""
        party_ctx = f"{pihak_kita} ({peran_kita})" if peran_kita else pihak_kita
        title = (req.clause_title or "Klausul Tambahan").strip()
        guidance = (req.advisory_guidance or "").strip()
        elems = [str(e).strip() for e in (req.recommended_elements or []) if str(e).strip()]
        deal_terms = (req.user_deal_terms or "").strip()
        elems_block = "\n".join(f"- {e}" for e in elems) if elems else "- Ketentuan hukum dan komersial standar yang melindungi Para Pihak."

        try:
            from services.llm_client import call_llm, extract_json
            if req.is_bilingual:
                prompt = (
                    "Anda adalah Konsultan Hukum Korporasi Indonesia senior.\n"
                    f"Buat draf klausul kontrak dwibahasa (Bahasa Indonesia & Bahasa Inggris) untuk topik: {title}.\n"
                    f"Lindungi kepentingan pihak kita: {party_ctx}.\n"
                    f"Panduan Hukum/Komersial: {guidance}\n"
                    f"Poin Wajib:\n{elems_block}\n"
                    f"Detail Kesepakatan Pengguna: {deal_terms or 'Gunakan standar perlindungan korporasi yang wajar.'}\n\n"
                    "Kembalikan HANYA JSON valid tanpa markdown:\n"
                    '{"draft_id": "<teks klausul lengkap Bahasa Indonesia dengan ayat (1), (2), ...>", '
                    '"draft_en": "<matching English legal translation with (1), (2), ...>"}'
                )
                raw = call_llm([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1100)
                parsed = extract_json(raw)
                d_id = str((parsed or {}).get("draft_id") or "").strip()
                d_en = str((parsed or {}).get("draft_en") or "").strip()
                if d_id:
                    return {
                        "rewritten": f"{d_id}\n\n{d_en}".strip() if d_en else d_id,
                        "draft_id": d_id,
                        "draft_en": d_en or None,
                    }
            else:
                prompt = (
                    "Anda adalah Konsultan Hukum Korporasi Indonesia senior.\n"
                    f"Buat draf klausul kontrak yang siap dimasukkan ke dalam perjanjian untuk topik: {title}.\n"
                    f"Lindungi kepentingan pihak kita: {party_ctx}.\n"
                    f"Panduan Hukum/Komersial: {guidance}\n"
                    f"Poin Wajib:\n{elems_block}\n"
                    f"Detail Kesepakatan Pengguna: {deal_terms or 'Gunakan standar perlindungan korporasi yang wajar.'}\n\n"
                    "Tulis dalam Bahasa Indonesia hukum yang tegas dengan penomoran ayat (1), (2), dst. "
                    "Berikan HANYA teks klausul tanpa pembuka atau penutup."
                )
                draft = call_llm([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=800)
                if draft and draft.strip():
                    return {"rewritten": draft.strip(), "draft_id": draft.strip(), "draft_en": None}
        except Exception:
            pass

        # Deterministic fallback when LLM is offline
        terms_note = f" Ketentuan khusus yang disepakati: {deal_terms}." if deal_terms else ""
        fallback_id = (
            f"Pasal Tambahan — {title}\n"
            f"(1) Para Pihak sepakat mengatur ketentuan mengenai {title} sesuai prinsip kehati-hatian dan kepatuhan hukum.{terms_note}\n"
            f"(2) Pelaksanaan ketentuan ini wajib memenuhi unsur: {'; '.join(elems) if elems else guidance}."
        )
        if req.is_bilingual:
            fallback_en = (
                f"Additional Article — {title}\n"
                f"(1) The Parties agree to govern {title} in accordance with prudent commercial practice and applicable law.{terms_note}\n"
                f"(2) Implementation shall cover: {'; '.join(elems) if elems else guidance}."
            )
            return {"rewritten": f"{fallback_id}\n\n{fallback_en}", "draft_id": fallback_id, "draft_en": fallback_en}
        return {"rewritten": fallback_id, "draft_id": fallback_id, "draft_en": None}

    clause = req.clause_text.strip()
    rec = (req.recommendation or "").strip()

    try:
        from services.llm_client import call_llm
        prompt = (
            "Anda adalah Konsultan Hukum Korporasi & Kepatuhan OJK. "
            "Tulis ulang klausul kontrak berikut dalam Bahasa Indonesia hukum yang tegas, jelas, dan patuh regulasi.\n\n"
            f"Fokus Perbaikan: {req.preset}\n"
            f"Rekomendasi Kepatuhan: {rec or 'Pastikan keseimbangan hak dan kewajiban serta kepatuhan OJK.'}\n"
            f"Klausul Asli:\n{clause}\n\n"
            "Berikan HANYA teks klausul hasil perbaikan tanpa pembuka atau penutup."
        )
        rewritten = call_llm([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=600)
        if rewritten and rewritten.strip():
            return {"rewritten": rewritten.strip()}
    except Exception:
        pass

    # Deterministic fallback when LLM is offline
    if req.preset == "balance":
        rewritten = (
            f"{clause} Pelaksanaan hak dan kewajiban sebagaimana dimaksud berlaku secara timbal balik "
            "(resiprokal) bagi Para Pihak dengan pemberitahuan tertulis sekurang-kurangnya 14 (empat belas) "
            "hari kalender sebelumnya."
        )
    elif req.preset == "clarify":
        rewritten = (
            f"Para Pihak sepakat bahwa: {clause} Ketentuan ini dilaksanakan dengan itikad baik, "
            "terukur, dan tunduk pada peraturan perundang-undangan yang berlaku di Republik Indonesia."
        )
    else:
        addition = rec if rec else (
            "Para Pihak wajib menjaga kerahasiaan data, melaksanakan pelaporan insiden secara tepat waktu, "
            "dan mematuhi seluruh ketentuan Peraturan Otoritas Jasa Keuangan (POJK) yang berlaku."
        )
        rewritten = f"{clause}\n\nKetentuan Kepatuhan Tambahan: {addition}"

    return {"rewritten": rewritten}


@router.post("/contracts/{report_id}/editor/recheck")
def recheck_editor_compliance(
    report_id: str,
    req: RecheckRequest,
    current_user: dict = Depends(get_current_user),
):
    import re

    verify_report_access(report_id, current_user["id"])
    row = pg_service.query_one(
        "SELECT id, results_json FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found")

    try:
        data = json.loads(row.get("results_json") or "{}")
    except Exception:
        data = {}

    summary = data.get("summary") or {}
    results = data.get("results") or []
    base_score = int(summary.get("skor_kepatuhan") or 0)

    plain = re.sub(r"<del\b[^>]*>.*?</del>", "", req.content_html, flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", " ", plain)

    risky_clauses = [r for r in results if r.get("status") in ("BERESIKO", "FATAL")]
    resolved_count = 0
    for r in risky_clauses:
        orig = (r.get("isi_pasal") or "").strip()
        if orig and orig not in plain:
            resolved_count += 1

    total_clauses = max(len(results), 1)
    bonus = round((resolved_count / total_clauses) * 100)
    new_score = min(100, base_score + bonus)

    return {
        "previous_score": base_score,
        "updated_score": new_score,
        "resolved_risks": resolved_count,
        "total_risks": len(risky_clauses),
    }


# ============================================================================
# Multi-User Collaboration & Document Versioning Endpoints
# ============================================================================


@router.get("/contracts/{report_id}/collaborators")
def get_collaborators(
    report_id: str,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])
    notebook_id = access.get("notebook_id") or report_id

    # Fetch owner details
    owner_user = pg_service.query_one(
        "SELECT id, username, email FROM users WHERE id = %s",
        (access["owner_id"],),
    )
    collaborators = []
    if owner_user:
        collaborators.append({
            "user_id": owner_user["id"],
            "username": owner_user["username"],
            "email": owner_user.get("email") or "",
            "color_index": 0,
            "is_owner": True,
            "role": "owner",
        })

    # Fetch collaborators
    collab_rows = pg_service.query(
        """
        SELECT wc.user_id, u.username, u.email, wc.color_index, wc.role
        FROM workspace_collaborators wc
        JOIN users u ON u.id = wc.user_id
        WHERE wc.notebook_id = %s
        ORDER BY wc.created_at ASC
        """,
        (notebook_id,),
    )
    for c in (collab_rows or []):
        collaborators.append({
            "user_id": c["user_id"],
            "username": c["username"],
            "email": c.get("email") or "",
            "color_index": c["color_index"],
            "is_owner": False,
            "role": c.get("role") or "editor",
        })

    return {
        "notebook_id": notebook_id,
        "is_owner": access["is_owner"],
        "my_color_index": access["color_index"],
        "collaborators": collaborators,
    }


@router.post("/contracts/{report_id}/collaborators")
def invite_collaborator(
    report_id: str,
    req: CollaboratorInviteRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])
    if not access["is_owner"]:
        raise HTTPException(status_code=403, detail="Hanya pemilik ruang kerja yang dapat mengundang kolaborator")

    ident = (req.identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="Identitas pengguna (email/username/ID) tidak boleh kosong")

    target_user = pg_service.query_one(
        "SELECT id, username, email FROM users WHERE LOWER(email) = LOWER(%s) OR LOWER(username) = LOWER(%s) OR id = %s",
        (ident, ident, ident),
    )
    if not target_user:
        raise HTTPException(status_code=404, detail="Pengguna tidak ditemukan dengan email, username, atau ID tersebut")

    if target_user["id"] == access["owner_id"]:
        raise HTTPException(status_code=400, detail="Pengguna adalah pemilik ruang kerja ini")

    notebook_id = access.get("notebook_id") or report_id
    existing = pg_service.query_one(
        "SELECT id FROM workspace_collaborators WHERE notebook_id = %s AND user_id = %s",
        (notebook_id, target_user["id"]),
    )
    if existing:
        raise HTTPException(status_code=400, detail="Pengguna sudah menjadi kolaborator di ruang kerja ini")

    # Pick next color_index in 1..5
    count_row = pg_service.query_one(
        "SELECT count(*) FROM workspace_collaborators WHERE notebook_id = %s",
        (notebook_id,),
    )
    cur_count = int(count_row.get("count", 0)) if count_row else 0
    next_color = (cur_count % 5) + 1

    collab_id = f"collab-{uuid.uuid4().hex[:10]}"
    pg_service.execute(
        """
        INSERT INTO workspace_collaborators (id, notebook_id, user_id, invited_by, role, color_index)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (collab_id, notebook_id, target_user["id"], current_user["id"], "editor", next_color),
    )

    return get_collaborators(report_id, current_user=current_user)


@router.delete("/contracts/{report_id}/collaborators/{target_user_id}")
def remove_collaborator(
    report_id: str,
    target_user_id: str,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])
    is_self = (current_user["id"] == target_user_id)
    if not access["is_owner"] and not is_self:
        raise HTTPException(status_code=403, detail="Tidak memiliki izin untuk menghapus kolaborator ini")

    if target_user_id == access["owner_id"]:
        raise HTTPException(status_code=400, detail="Pemilik ruang kerja tidak dapat dihapus")

    notebook_id = access.get("notebook_id") or report_id
    pg_service.execute(
        "DELETE FROM workspace_collaborators WHERE notebook_id = %s AND user_id = %s",
        (notebook_id, target_user_id),
    )
    pg_service.execute(
        "DELETE FROM workspace_presence WHERE report_id = %s AND user_id = %s",
        (report_id, target_user_id),
    )
    return {"status": "removed", "user_id": target_user_id}


@router.post("/contracts/{report_id}/editor/heartbeat")
def editor_heartbeat(
    report_id: str,
    req: EditorHeartbeatRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])

    # Prune presence older than 12 seconds
    pg_service.execute(
        "DELETE FROM workspace_presence WHERE report_id = %s AND last_seen < now() - interval '12 seconds'",
        (report_id,),
    )

    # Upsert current user presence
    username = current_user.get("username") or current_user.get("name") or "Pengguna"
    pg_service.execute(
        """
        INSERT INTO workspace_presence
            (report_id, user_id, username, color_index, is_typing, active_clause, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (report_id, user_id) DO UPDATE SET
            username = EXCLUDED.username,
            color_index = EXCLUDED.color_index,
            is_typing = EXCLUDED.is_typing,
            active_clause = EXCLUDED.active_clause,
            last_seen = now()
        """,
        (
            report_id,
            current_user["id"],
            username,
            access["color_index"],
            bool(req.is_typing),
            req.active_clause,
        ),
    )

    active_rows = pg_service.query(
        """
        SELECT user_id, username, color_index, is_typing, active_clause
        FROM workspace_presence
        WHERE report_id = %s
        ORDER BY last_seen DESC
        """,
        (report_id,),
    )

    latest_draft = None
    if req.known_updated_at is not None:
        draft = pg_service.query_one(
            "SELECT d.content_html, d.version, d.user_id, u.username AS editor_name, "
            "to_char(d.updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS updated_at "
            "FROM document_editor_drafts d "
            "LEFT JOIN users u ON u.id = d.user_id "
            "WHERE d.report_id = %s",
            (report_id,),
        )
        if (
            draft
            and draft.get("updated_at")
            and draft["updated_at"] != req.known_updated_at
            and draft.get("user_id") != current_user["id"]
        ):
            latest_draft = {
                "content_html": draft["content_html"],
                "version": draft["version"],
                "editor_name": draft.get("editor_name"),
                "editor_id": draft.get("user_id"),
                "updated_at": draft["updated_at"],
            }

    return {
        "active_users": [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "color_index": r["color_index"],
                "is_typing": bool(r.get("is_typing")),
                "active_clause": r.get("active_clause"),
            }
            for r in (active_rows or [])
        ],
        "latest_draft": latest_draft,
    }


@router.get("/contracts/{report_id}/editor/versions")
def list_editor_versions(
    report_id: str,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])
    rows = pg_service.query(
        """
        SELECT id, version_number, title, author_name, color_index, changes_json,
               to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS created_at
        FROM document_editor_versions
        WHERE report_id = %s
        ORDER BY version_number DESC
        """,
        (report_id,),
    )
    versions = []
    for r in (rows or []):
        changes = []
        if r.get("changes_json"):
            try:
                changes = json.loads(r["changes_json"])
            except Exception:
                changes = []
        versions.append({
            "id": r["id"],
            "version_number": r["version_number"],
            "title": r["title"],
            "author_name": r["author_name"],
            "color_index": r["color_index"],
            "changes": changes,
            "created_at": r["created_at"],
        })
    return {"versions": versions}


@router.post("/contracts/{report_id}/editor/versions")
def create_editor_version(
    report_id: str,
    req: CreateVersionRequest,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    access = verify_report_access(report_id, current_user["id"])

    next_ver_row = pg_service.query_one(
        "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_ver FROM document_editor_versions WHERE report_id = %s",
        (report_id,),
    )
    next_ver = int(next_ver_row["next_ver"]) if next_ver_row else 1
    title = (req.title or "").strip() or f"Versi {next_ver}"
    author_name = current_user.get("username") or current_user.get("name") or "Pengguna"
    color_index = access["color_index"]
    ver_id = f"ver-{uuid.uuid4().hex[:12]}"
    changes_json = json.dumps(req.changes or [])

    pg_service.execute(
        """
        INSERT INTO document_editor_versions
            (id, report_id, version_number, title, snapshot_html, created_by, author_name, color_index, changes_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            ver_id,
            report_id,
            next_ver,
            title,
            req.content_html,
            current_user["id"],
            author_name,
            color_index,
            changes_json,
        ),
    )

    # Clean active highlights if requested (e.g. baking clean document)
    content_to_save = req.content_html
    if req.clean_highlights:
        content_to_save = re.sub(
            r'<ins\b[^>]*class="[^"]*ed-suggest-ins[^"]*"[^>]*>(.*?)</ins>',
            r'\1',
            content_to_save,
            flags=re.DOTALL | re.IGNORECASE,
        )
        content_to_save = re.sub(
            r'<del\b[^>]*class="[^"]*ed-suggest-del[^"]*"[^>]*>.*?</del>',
            '',
            content_to_save,
            flags=re.DOTALL | re.IGNORECASE,
        )

    draft_id = f"draft-{uuid.uuid4().hex[:12]}"
    pg_service.execute(
        """
        INSERT INTO document_editor_drafts (id, report_id, user_id, content_html, version, updated_at)
        VALUES (%s, %s, %s, %s, 1, now())
        ON CONFLICT (report_id) DO UPDATE SET
            content_html = EXCLUDED.content_html,
            version = document_editor_drafts.version + 1,
            updated_at = now()
        """,
        (draft_id, report_id, current_user["id"], content_to_save),
    )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": ver_id,
        "version_number": next_ver,
        "title": title,
        "author_name": author_name,
        "color_index": color_index,
        "changes": req.changes or [],
        "created_at": now_iso,
    }


@router.post("/contracts/{report_id}/editor/versions/{version_id}/restore")
def restore_editor_version(
    report_id: str,
    version_id: str,
    current_user: dict = Depends(get_current_user),
):
    _ensure_editor_tables()
    verify_report_access(report_id, current_user["id"])

    ver = pg_service.query_one(
        "SELECT version_number, snapshot_html FROM document_editor_versions WHERE id = %s AND report_id = %s",
        (version_id, report_id),
    )
    if not ver:
        raise HTTPException(status_code=404, detail="Versi dokumen tidak ditemukan")

    draft_id = f"draft-{uuid.uuid4().hex[:12]}"
    pg_service.execute(
        """
        INSERT INTO document_editor_drafts (id, report_id, user_id, content_html, version, updated_at)
        VALUES (%s, %s, %s, %s, 1, now())
        ON CONFLICT (report_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            content_html = EXCLUDED.content_html,
            version = document_editor_drafts.version + 1,
            updated_at = now()
        """,
        (draft_id, report_id, current_user["id"], ver["snapshot_html"]),
    )
    doc_notifications.notify_collaborators(report_id, current_user["id"], event_type="document_edited")

    updated_draft = pg_service.query_one(
        "SELECT version, to_char(updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS updated_at FROM document_editor_drafts WHERE report_id = %s",
        (report_id,),
    )

    return {
        "status": "restored",
        "version_id": version_id,
        "restored_version_number": ver["version_number"],
        "content_html": ver["snapshot_html"],
        "draft_version": updated_draft["version"] if updated_draft else 1,
        "updated_at": updated_draft["updated_at"] if updated_draft else None,
    }


@router.patch("/contracts/{report_id}/missing-clauses/{gap_id}")
def update_missing_clause_status(
    report_id: str,
    gap_id: str,
    req: MissingClauseStatusRequest,
    current_user: dict = Depends(get_current_user),
):
    verify_report_access(report_id, current_user["id"])
    allowed_statuses = {"open", "addressed", "dismissed"}
    new_status = (req.resolution_status or "").strip().lower()
    if new_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid resolution_status; must be open, addressed, or dismissed")

    row = pg_service.query_one(
        "SELECT id, results_json FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found")

    try:
        data = json.loads(row.get("results_json") or "{}")
    except Exception:
        data = {}

    gaps = data.get("missing_clauses")
    if not isinstance(gaps, list):
        raise HTTPException(status_code=404, detail="Missing clause not found")

    updated_gap = None
    for gap in gaps:
        if isinstance(gap, dict) and gap.get("gap_id") == gap_id:
            gap["resolution_status"] = new_status
            if new_status == "dismissed":
                gap["dismissed_reason"] = (req.dismissed_reason or "").strip() or None
            elif new_status == "open":
                gap["dismissed_reason"] = None
            updated_gap = gap
            break

    if not updated_gap:
        raise HTTPException(status_code=404, detail="Missing clause gap_id not found")

    pg_service.execute(
        "UPDATE compliance_history SET results_json = %s WHERE id = %s",
        (json.dumps(data), report_id),
    )
    return {"gap": updated_gap}


@router.post("/contracts/{report_id}/recheck-gaps")
def recheck_missing_clauses(
    report_id: str,
    req: RecheckGapsRequest,
    current_user: dict = Depends(get_current_user),
):
    verify_report_access(report_id, current_user["id"])
    row = pg_service.query_one(
        "SELECT id, results_json FROM compliance_history WHERE id = %s",
        (report_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found")

    try:
        data = json.loads(row.get("results_json") or "{}")
    except Exception:
        data = {}

    draft_row = pg_service.query_one(
        "SELECT content_html FROM document_editor_drafts WHERE report_id = %s",
        (report_id,),
    )

    from services import gap_detector
    pasal_items: list[dict] = []
    if draft_row and draft_row.get("content_html"):
        pasal_items = gap_detector.reconstruct_pasal_items_from_html(draft_row["content_html"])

    if not pasal_items:
        for r in (data.get("results") or []):
            if isinstance(r, dict) and r.get("isi_pasal"):
                pasal_items.append({
                    "pasal": r.get("pasal") or "Klausul",
                    "deskripsi": r.get("isi_pasal") or "",
                })

    summary = data.get("summary") or {}
    is_bilingual = bool(data.get("layout_mode") == "bilingual_parallel" or data.get("segments"))
    full_text = "\n\n".join(f"{p.get('pasal', '')}\n{p.get('deskripsi', '')}" for p in pasal_items)

    archetype, missing_clauses = gap_detector.detect_missing_clauses(
        pasal_items=pasal_items,
        contract_type=req.contract_type,
        doc_type=str(summary.get("jenis_dokumen") or ""),
        pihak_kita=str(summary.get("pihak_kita") or summary.get("pihak_pertama") or ""),
        peran_kita=summary.get("peran_kita"),
        is_bilingual=is_bilingual,
        full_text=full_text,
        existing_gaps=data.get("missing_clauses") or [],
    )

    data["detected_contract_type"] = archetype
    data["missing_clauses"] = missing_clauses
    if isinstance(summary, dict):
        summary["detected_contract_type"] = archetype
        summary["missing_clauses_count"] = len(missing_clauses)
        data["summary"] = summary

    pg_service.execute(
        "UPDATE compliance_history SET results_json = %s WHERE id = %s",
        (json.dumps(data), report_id),
    )
    return {
        "detected_contract_type": archetype,
        "missing_clauses": missing_clauses,
    }


@router.post("/contracts/{report_id}/mark-read", status_code=204)
def mark_contract_read(
    report_id: str,
    current_user: dict = Depends(get_current_user),
):
    verify_report_access(report_id, current_user["id"])
    doc_notifications.mark_document_as_read(report_id, current_user["id"])
    return None
    if not row:
        raise HTTPException(status_code=404, detail="Document report not found")

    try:
        data = json.loads(row.get("results_json") or "{}")
    except Exception:
        data = {}

    summary = data.get("summary") or {}
    results = data.get("results") or []
    base_score = int(summary.get("skor_kepatuhan") or 0)

    plain = re.sub(r"<del\b[^>]*>.*?</del>", "", req.content_html, flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", " ", plain)

    risky_clauses = [r for r in results if r.get("status") in ("BERESIKO", "FATAL")]
    resolved_count = 0
    for r in risky_clauses:
        orig = (r.get("isi_pasal") or "").strip()
        if orig and orig not in plain:
            resolved_count += 1

    total_clauses = max(len(results), 1)
    bonus = round((resolved_count / total_clauses) * 100)
    new_score = min(100, base_score + bonus)

    return {
        "previous_score": base_score,
        "updated_score": new_score,
        "resolved_risks": resolved_count,
        "total_risks": len(risky_clauses),
    }
