"""Layout-aware extractor and analyzer for side-by-side bilingual (ID / EN) contracts."""

from __future__ import annotations

import html
import re
from typing import Optional

import docx
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
import fitz

_ID_STOPWORDS = {
    "yang", "dan", "dengan", "untuk", "perjanjian", "pihak", "pasal", "bahwa",
    "atas", "atau", "sebagaimana", "ketentuan", "dalam", "dari", "oleh", "kepada",
    "tersebut", "ini", "para", "dapat", "wajib", "harus", "layanan", "syarat",
}

_EN_STOPWORDS = {
    "the", "and", "with", "for", "agreement", "party", "parties", "article",
    "that", "shall", "must", "may", "terms", "conditions", "services", "from",
    "this", "such", "under", "between", "herein", "thereof", "provided", "above",
}

_OBLIGATION_EN_STRONG = re.compile(r"\b(shall|must|is\s+obligated\s+to|will\s+be\s+required\s+to)\b", re.I)
_OBLIGATION_ID_WEAK = re.compile(r"\b(dapat|berhak|boleh)\b", re.I)
_OBLIGATION_ID_STRONG = re.compile(r"\b(wajib|harus|berkewajiban)\b", re.I)
_OBLIGATION_EN_WEAK = re.compile(r"\b(may|is\s+entitled\s+to|can)\b", re.I)
_NUM_TOKEN_RE = re.compile(r"(?:rp\.?|idr|usd|\$)?\s*\d+(?:[.,]\d+)*\s*%?", re.I)


def clean_bilingual_cell(raw: str) -> str:
    """Rejoin mid-word line-break hyphens while preserving spaced hyphens (e.g. 'hal - hal')."""
    if not raw:
        return ""
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"([A-Za-zÀ-ÿ])-\s*\n\s*([A-Za-zÀ-ÿ])", r"\1\2", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _lang_scores(text: str) -> tuple[float, float]:
    words = re.findall(r"[A-Za-z]+", (text or "").lower())
    if not words:
        return 0.0, 0.0
    id_hits = sum(1 for w in words if w in _ID_STOPWORDS)
    en_hits = sum(1 for w in words if w in _EN_STOPWORDS)
    total_hits = id_hits + en_hits
    if total_hits == 0:
        return 0.0, 0.0
    return id_hits / total_hits, en_hits / total_hits


def detect_bilingual_columns(rows: list[list[str]]) -> tuple[bool, Optional[list[str]]]:
    """Detect whether a 2-column table is parallel bilingual (EN/ID or ID/EN).

    Returns ``(True, ["en", "id"])`` or ``(True, ["id", "en"])`` if detected,
    otherwise ``(False, None)``.
    """
    col0_parts: list[str] = []
    col1_parts: list[str] = []

    for row in rows or []:
        if not row or len(row) != 2:
            continue
        c0 = clean_bilingual_cell(row[0] or "")
        c1 = clean_bilingual_cell(row[1] or "")
        if not c0 or not c1 or c0 == c1:
            continue
        col0_parts.append(c0)
        col1_parts.append(c1)

    if not col0_parts:
        return False, None

    id0, en0 = _lang_scores(" ".join(col0_parts))
    id1, en1 = _lang_scores(" ".join(col1_parts))

    if en0 >= 0.60 and id1 >= 0.60:
        return True, ["en", "id"]
    if id0 >= 0.60 and en1 >= 0.60:
        return True, ["id", "en"]
    return False, None


def detect_governing_language(segments: list[dict]) -> str:
    """Resolve governing language from contract text; defaults to 'id' per UU No. 24/2009."""
    for seg in segments:
        combined = f"{seg.get('en', '')} {seg.get('id', '')} {seg.get('text', '')}".lower()
        if any(kw in combined for kw in ("governing language", "prevail", "bahasa yang berlaku", "mengikat")):
            if re.search(r"(english\s+(?:version|text|language)\s+shall\s+prevail|bahasa\s+inggris\s+yang\s+berlaku)", combined):
                return "en"
            if re.search(r"(indonesian\s+(?:version|text|language)\s+shall\s+prevail|bahasa\s+indonesia\s+yang\s+berlaku)", combined):
                return "id"
    return "id"


def _is_section_header(c0: str, c1: str) -> bool:
    if not c0 and not c1:
        return False
    if c0 == c1:
        return True
    if re.match(r"^(article|pasal|bab|chapter)\s+\d+", c0, re.I) and len(c0) < 120:
        return True
    if re.match(r"^(article|pasal|bab|chapter)\s+\d+", c1, re.I) and len(c1) < 120:
        return True
    return False


_BORDER_EDGES = ("top", "left", "bottom", "right", "insideH", "insideV")
_INVISIBLE_BORDER_VALS = {"none", "nil"}


def _is_border_el_visible(el) -> bool:
    if el is None:
        return False
    val = (el.get(qn("w:val")) or "single").lower()
    if val in _INVISIBLE_BORDER_VALS:
        return False
    try:
        sz = int(el.get(qn("w:sz")) or "4")
    except ValueError:
        sz = 4
    return sz > 0


def is_docx_table_borderless(table: Table) -> bool:
    """Return True when a Word table has no visible table-style, table, or cell borders."""
    effective_tbl: dict[str, bool] = {}

    style = getattr(table, "style", None)
    if style is not None and getattr(style, "element", None) is not None:
        for el in style.element.xpath("./w:tblPr/w:tblBorders/*"):
            edge = el.tag.rsplit("}", 1)[-1]
            if edge in _BORDER_EDGES:
                effective_tbl[edge] = _is_border_el_visible(el)

    tbl_pr = table._tbl.tblPr
    if tbl_pr is not None:
        tbl_borders = tbl_pr.find(qn("w:tblBorders"))
        if tbl_borders is not None:
            for edge in _BORDER_EDGES:
                el = tbl_borders.find(qn(f"w:{edge}"))
                if el is not None:
                    effective_tbl[edge] = _is_border_el_visible(el)

    if any(effective_tbl.values()):
        return False

    for row in table.rows:
        for cell in row.cells:
            tc_pr = cell._tc.tcPr
            tc_borders = tc_pr.find(qn("w:tcBorders")) if tc_pr is not None else None
            if tc_borders is not None:
                for edge in ("top", "left", "bottom", "right"):
                    el = tc_borders.find(qn(f"w:{edge}"))
                    if _is_border_el_visible(el):
                        return False

    return True


def render_bilingual_table_html(
    rows: list[list[str]],
    column_order: list[str],
    segment_start_idx: int = 1,
    borderless: bool = False,
) -> tuple[str, int]:
    """Serialize a 2-column bilingual table into semantic editor HTML."""
    order = column_order if column_order in (["en", "id"], ["id", "en"]) else ["en", "id"]
    cls = "ed-doc-table bilingual-contract-table is-borderless" if borderless else "ed-doc-table bilingual-contract-table"
    border_attr = ' data-borderless="true"' if borderless else ""
    out = [f'<table class="{cls}"{border_attr} data-column-order="{order[0]},{order[1]}"><tbody>']
    seg_idx = segment_start_idx

    for row in rows:
        if not row:
            continue
        if len(row) == 1 or (len(row) >= 2 and clean_bilingual_cell(row[0]) == clean_bilingual_cell(row[1])):
            txt = clean_bilingual_cell(row[0] or "")
            if not txt:
                continue
            seg_id = f"seg_{seg_idx:03d}"
            seg_idx += 1
            out.append(
                f'<tr class="bilingual-section-row" data-segment-id="{seg_id}">'
                f'<td colspan="2"><strong>{html.escape(txt)}</strong></td></tr>'
            )
            continue

        c0 = clean_bilingual_cell(row[0] or "")
        c1 = clean_bilingual_cell(row[1] or "")
        if not c0 and not c1:
            continue

        seg_id = f"seg_{seg_idx:03d}"
        seg_idx += 1
        lang0, lang1 = order[0], order[1]
        h0 = html.escape(c0).replace("\n\n", "<br/><br/>").replace("\n", "<br/>")
        h1 = html.escape(c1).replace("\n\n", "<br/><br/>").replace("\n", "<br/>")
        out.append(
            f'<tr class="bilingual-clause-row" data-segment-id="{seg_id}">'
            f'<td class="col-{lang0}" lang="{lang0}">{h0}</td>'
            f'<td class="col-{lang1}" lang="{lang1}">{h1}</td>'
            f'</tr>'
        )

    out.append("</tbody></table>")
    return "".join(out), seg_idx


def _stitch_split_segments(segments: list[dict]) -> list[dict]:
    """Merge bilingual rows split across PDF page breaks."""
    if not segments:
        return segments
    stitched: list[dict] = []
    for seg in segments:
        if (
            stitched
            and stitched[-1].get("type") == "paired_clause"
            and seg.get("type") == "paired_clause"
            and seg.get("page", 0) > stitched[-1].get("page", 0)
        ):
            prev = stitched[-1]
            prev_en = prev.get("en", "")
            prev_id = prev.get("id", "")
            cur_en = seg.get("en", "")
            cur_id = seg.get("id", "")
            prev_incomplete = (prev_en and prev_en[-1] not in ".:;") or (prev_id and prev_id[-1] not in ".:;")
            cur_cont = (cur_en and cur_en[0].islower()) or (cur_id and cur_id[0].islower())
            if prev_incomplete and cur_cont:
                prev["en"] = f"{prev_en} {cur_en}".strip()
                prev["id"] = f"{prev_id} {cur_id}".strip()
                continue
        stitched.append(seg)
    return stitched


def extract_bilingual_from_docx(docx_path: str) -> dict:
    """Extract canonical bilingual segments from a DOCX file in document order."""
    doc = docx.Document(docx_path)
    segments: list[dict] = []
    detected_order: Optional[list[str]] = None
    saw_borderless_table = False
    saw_bordered_table = False
    seg_idx = 1

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            txt = clean_bilingual_cell(Paragraph(child, doc).text or "")
            if txt:
                segments.append({
                    "segment_id": f"seg_{seg_idx:03d}",
                    "type": "standalone_block",
                    "text": txt,
                    "page": 1,
                })
                seg_idx += 1
        elif tag == "tbl":
            tbl = Table(child, doc)
            rows = [[clean_bilingual_cell(c.text or "") for c in row.cells] for row in tbl.rows]
            is_bi, order = detect_bilingual_columns(rows)
            if is_bi and order:
                if not detected_order:
                    detected_order = order
                if is_docx_table_borderless(tbl):
                    saw_borderless_table = True
                else:
                    saw_bordered_table = True
                en_idx = order.index("en")
                id_idx = order.index("id")
                for r in rows:
                    if len(r) < 2:
                        continue
                    en_txt = r[en_idx]
                    id_txt = r[id_idx]
                    if not en_txt and not id_txt:
                        continue
                    seg_type = "section_header" if _is_section_header(en_txt, id_txt) else "paired_clause"
                    segments.append({
                        "segment_id": f"seg_{seg_idx:03d}",
                        "type": seg_type,
                        "en": en_txt,
                        "id": id_txt,
                        "page": 1,
                    })
                    seg_idx += 1

    is_bilingual = detected_order is not None
    border_style = "borderless" if (saw_borderless_table and not saw_bordered_table) else "bordered"
    source_layout = f"table_{border_style}" if is_bilingual else "monolingual"
    governing_language = detect_governing_language(segments) if is_bilingual else "id"
    return {
        "is_bilingual": is_bilingual,
        "layout_mode": "bilingual_parallel" if is_bilingual else "monolingual",
        "source_layout": source_layout,
        "border_style": border_style,
        "column_order": detected_order,
        "governing_language": governing_language,
        "segments": segments,
    }


def extract_bilingual_from_pdf(pdf_path: str) -> dict:
    """Extract canonical bilingual segments from a PDF file (bordered + borderless fallback)."""
    doc = fitz.open(pdf_path)
    segments: list[dict] = []
    detected_order: Optional[list[str]] = None
    saw_borderless_table = False
    saw_bordered_table = False
    seg_idx = 1

    for page_num, page in enumerate(doc, start=1):
        tables = []
        used_text_fallback = False
        try:
            tabs = page.find_tables()
            if tabs and tabs.tables:
                tables = tabs.tables
        except Exception:
            tables = []

        if not tables:
            try:
                tabs_fb = page.find_tables(vertical_strategy="text", horizontal_strategy="text")
                if tabs_fb and tabs_fb.tables:
                    tables = tabs_fb.tables
                    used_text_fallback = True
            except Exception:
                tables = []

        table_bboxes = []
        page_items: list[tuple[float, dict]] = []

        for t in tables:
            try:
                raw_rows = t.extract()
            except Exception:
                raw_rows = None
            if not raw_rows or len(raw_rows) < 1 or len(raw_rows[0]) != 2:
                continue
            rows = [[clean_bilingual_cell(c or "") for c in r] for r in raw_rows]
            is_bi, order = detect_bilingual_columns(rows)
            if not is_bi or not order:
                continue
            if not detected_order:
                detected_order = order
            if used_text_fallback:
                saw_borderless_table = True
            else:
                saw_bordered_table = True
            table_bboxes.append(t.bbox)
            en_idx = order.index("en")
            id_idx = order.index("id")
            for r in rows:
                en_txt = r[en_idx]
                id_txt = r[id_idx]
                if not en_txt and not id_txt:
                    continue
                seg_type = "section_header" if _is_section_header(en_txt, id_txt) else "paired_clause"
                page_items.append((
                    t.bbox[1],
                    {
                        "segment_id": f"seg_{seg_idx:03d}",
                        "type": seg_type,
                        "en": en_txt,
                        "id": id_txt,
                        "page": page_num,
                    },
                ))
                seg_idx += 1

        for b in page.get_text("blocks"):
            bbox = b[:4]
            cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            if any(tb[0] - 3 <= cx <= tb[2] + 3 and tb[1] - 3 <= cy <= tb[3] + 3 for tb in table_bboxes):
                continue
            txt = clean_bilingual_cell(b[4] if len(b) > 4 else "")
            if txt:
                page_items.append((
                    bbox[1],
                    {
                        "segment_id": f"seg_{seg_idx:03d}",
                        "type": "standalone_block",
                        "text": txt,
                        "page": page_num,
                    },
                ))
                seg_idx += 1

        page_items.sort(key=lambda item: item[0])
        segments.extend(item[1] for item in page_items)

    segments = _stitch_split_segments(segments)
    is_bilingual = detected_order is not None
    border_style = "borderless" if (saw_borderless_table and not saw_bordered_table) else "bordered"
    source_layout = f"table_{border_style}" if is_bilingual else "monolingual"
    governing_language = detect_governing_language(segments) if is_bilingual else "id"
    return {
        "is_bilingual": is_bilingual,
        "layout_mode": "bilingual_parallel" if is_bilingual else "monolingual",
        "source_layout": source_layout,
        "border_style": border_style,
        "column_order": detected_order,
        "governing_language": governing_language,
        "segments": segments,
    }


def find_translation_discrepancies(segments: list[dict]) -> list[dict]:
    """Flag obligation shifts, numeric/currency mismatches, and missing translations."""
    discrepancies: list[dict] = []
    for seg in segments:
        if seg.get("type") != "paired_clause":
            continue
        en = (seg.get("en") or "").strip()
        id_txt = (seg.get("id") or "").strip()
        seg_id = seg.get("segment_id") or ""

        if bool(en) != bool(id_txt):
            discrepancies.append({
                "segment_id": seg_id,
                "severity": "high",
                "category": "missing_translation",
                "en_excerpt": en[:180],
                "id_excerpt": id_txt[:180],
                "explanation": "One language column is empty while the parallel column contains text.",
            })
            continue

        if _OBLIGATION_EN_STRONG.search(en) and _OBLIGATION_ID_WEAK.search(id_txt) and not _OBLIGATION_ID_STRONG.search(id_txt):
            discrepancies.append({
                "segment_id": seg_id,
                "severity": "high",
                "category": "obligation_mismatch",
                "en_excerpt": en[:180],
                "id_excerpt": id_txt[:180],
                "explanation": "English uses mandatory obligation ('shall'/'must'), whereas Indonesian uses permissive phrasing ('dapat'/'berhak').",
            })
        elif _OBLIGATION_ID_STRONG.search(id_txt) and _OBLIGATION_EN_WEAK.search(en) and not _OBLIGATION_EN_STRONG.search(en):
            discrepancies.append({
                "segment_id": seg_id,
                "severity": "high",
                "category": "obligation_mismatch",
                "en_excerpt": en[:180],
                "id_excerpt": id_txt[:180],
                "explanation": "Indonesian uses mandatory obligation ('wajib'/'harus'), whereas English uses permissive phrasing ('may'/'can').",
            })

        en_nums = {re.sub(r"\s+", "", m.lower()) for m in _NUM_TOKEN_RE.findall(en)}
        id_nums = {re.sub(r"\s+", "", m.lower()) for m in _NUM_TOKEN_RE.findall(id_txt)}
        if en_nums and id_nums and en_nums != id_nums:
            # Ignore simple comma/dot decimal formatting differences (e.g. 10.5 vs 10,5)
            norm_en = {n.replace(",", ".") for n in en_nums}
            norm_id = {n.replace(",", ".") for n in id_nums}
            if norm_en != norm_id:
                discrepancies.append({
                    "segment_id": seg_id,
                    "severity": "medium",
                    "category": "numeric_mismatch",
                    "en_excerpt": en[:180],
                    "id_excerpt": id_txt[:180],
                    "explanation": f"Numeric or currency values differ between EN ({sorted(norm_en)}) and ID ({sorted(norm_id)}).",
                })

    return discrepancies


def build_governing_text(segments: list[dict], governing_language: str = "id") -> str:
    """Build a single-stream governing-language text with [seg_id] prefixes for compliance RAG."""
    lang = governing_language if governing_language in ("id", "en") else "id"
    lines: list[str] = []
    for seg in segments:
        sid = seg.get("segment_id") or ""
        if seg.get("type") == "standalone_block":
            txt = seg.get("text") or ""
        else:
            txt = seg.get(lang) or seg.get("id") or seg.get("en") or ""
        if txt:
            lines.append(f"[{sid}] {txt}" if sid else txt)
    return "\n\n".join(lines)
