"""RAG core: pgvector retrieval, cross-encoder reranking, hybrid retrieval,
and the document text-extraction / understanding pipeline.

Moved from api/main.py during the Phase 2 refactor. LLM calls go through
services.llm_client. Migration M3 (cutover): dense retrieval targets the PG
`chunks` table (pgvector HNSW, cosine) instead of the ChromaDB
'ojk_regulations' collection, and the sparse half uses PG full-text search
on chunks.content_tsv instead of SQLite FTS5 chunks_fts.
"""
import os
import re
import json
import threading
from typing import List
from datetime import datetime
import auth
from services import pg_service
from services import grant_policy
from services.embed_service import embed_query
from services.llm_client import call_llm, call_llm_vision, vlm_extract_page_image, VLM_PAGE_PROMPT, extract_json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Migration M3: get_chroma_collection() removed -- the vector store is the
# PG `chunks` table now. Former callers (repository.py, knowledge_graph.py,
# and retrieve_contexts below) query PG directly via services.pg_service.

_cross_encoder = None
_reranker_lock = threading.Lock()

# Guardrail (2026-09-29 RAG audit): chunk rows are not size-bounded -- the
# corpus contains a single 8.8 MB "chunk" (doc 653) and ~140 chunks >25k
# chars. Uncapped text reaching retrieval output would be fed verbatim to the
# cross-encoder tokenizer, the LLM prompt (chat.py concatenates every source
# text) and the FE snippet field. Contexts are grounding passages, not whole
# documents, so they are capped at the single point where the hybrid path
# builds source text. The CAG early-return has its own documented 25k
# whole-document budget and is intentionally left alone.
MAX_CONTEXT_CHARS = 6000


def _cap_context_text(text):
    """Truncate oversized chunk text used as LLM/reranker/UI context."""
    if text and len(text) > MAX_CONTEXT_CHARS:
        return text[:MAX_CONTEXT_CHARS] + " […]"
    return text


# ── B2 reference-base exclusion set ─────────────────────────────────────────
# Per-user DESELECTED sources (migration 015). Purely subtractive: a ref in the
# set only removes a candidate from retrieval, never adds or surfaces one, so an
# empty set reproduces current behavior byte-for-byte.
def get_user_exclusions(user_id) -> set:
    try:
        return {r["source_ref"] for r in pg_service.query(
            "SELECT source_ref FROM rag_source_exclusions WHERE user_id = %s",
            (str(user_id),))}
    except Exception as e:
        print(f"Error fetching rag exclusions: {e}")
        return set()   # fail-open: subtractive-only, so this can only fail to
                       # exclude a deselected source, never leak an unauthorized one.


def chunk_source_ref(doc_id, chunk_id) -> str | None:
    """Map a chunks row to its exclusion source_ref, or None (never excluded).

    Public law: 'reg:<doc_id>' (doc_id is already regulations.id::text).
    Internal upload: 'internal:<upload_id>' from the internal_<upload_id>_<i>
    chunk-id shape in internal_docs.py — split('_')[1] MUST match the
    /rag-sources/list split_part(id,'_',2) derivation. The startswith guard
    makes the split index-safe (an internal_ id always contains a '_').
    """
    if doc_id:
        return f"reg:{doc_id}"
    if chunk_id and chunk_id.startswith("internal_"):
        return f"internal:{chunk_id.split('_')[1]}"
    return None


def get_reranker():
    # Double-checked locking (2026-09-29 RAG audit): the startup warm thread
    # and a concurrent first query must not load the model twice.
    global _cross_encoder
    if _cross_encoder is None:
        with _reranker_lock:
            if _cross_encoder is None:
                import os
                from sentence_transformers import CrossEncoder
                model_name = os.environ.get("RERANKER_MODEL_NAME", "cross-encoder/ms-marco-MiniLM-L-6-v2")
                print(f"Initializing CrossEncoder reranker: {model_name}")
                _cross_encoder = CrossEncoder(model_name, max_length=512)
    return _cross_encoder

def _normalize_clause_label(raw_label: str) -> str:
    cleaned = raw_label.strip()

    # If first_line has a clean title with separator like "Lampiran 1: Service Level Agreement" or "Pasal 1: Definisi"
    if any(sep in cleaned for sep in [":", " - ", " – "]) and len(cleaned) <= 80:
        return cleaned

    # Normalize OCR-corrupted Pasal numbers: e.g. 2I -> 21, T3 -> 13, IO -> 10, PASAI,6 -> Pasal 6
    m = re.match(r'^(?:pasal|pasa[il1]|article)[,\s]+([0-9a-z]+)', cleaned, re.I)
    if m:
        num = m.group(1).upper()
        if re.match(r'^[IVXLCDM]+$', num):
            return f"Pasal {num}"
        num_clean = (
            num.replace('O', '0')
               .replace('I', '1')
               .replace('L', '1')
               .replace('T', '1')
               .replace('S', '5')
               .replace('B', '8')
        )
        if num_clean.isdigit():
            return f"Pasal {num_clean}"
        return f"Pasal {num}"

    # Normalize OCR-corrupted Lampiran headers: e.g. I.AM?IRAN I -> Lampiran I, I/TMPIRAN tr -> Lampiran II
    m_lamp = re.match(
        r'^(?:lampiran|l[a\.\/\?]+m[p\?]+iran|i[\.\/]?[a\.\/\?]*t?m[p\?]+iran|annex|appendix)(?:\s+([0-9a-z]+))?',
        cleaned,
        re.I
    )
    if m_lamp:
        suffix = (m_lamp.group(1) or "").upper()
        if suffix in ["TR"]:
            suffix = "II"
        if suffix:
            return f"Lampiran {suffix}"
        return "Lampiran"

    # Procedural flowchart / problem reporting
    if re.search(r'problem\s+reportin[gq]|prosedur|procedure', cleaned, re.I):
        return "Prosedur: Pelaporan & Eskalasi"

    # Tables & Matrices
    if re.match(r'^tabel\b', cleaned, re.I):
        return cleaned[:50].strip().title()
    if re.match(r'^matri[kx]s?\b', cleaned, re.I):
        return cleaned[:50].strip().title()
    if re.search(r'service\s+(?:level|lrvel|ievel)\s+agreement', cleaned, re.I):
        return "Lampiran: Service Level Agreement (SLA)"
    if re.match(r'^(?:formulir|form)\b', cleaned, re.I):
        return cleaned[:50].strip().title()

    return cleaned[:60].strip().title()


_PROSE_WORDS_RE = re.compile(
    r'\b(?:yang\s+ditandatangani|sebagaimana|merupakan|adalah|tersebut|dimaksud|berdasarkan|apabila|dalam\s+hal|ayat\s*\(\d+\)|ayat\s+\d+|tidak\s+terpisahkan)\b',
    re.I
)
_PREV_PREPOSITION_RE = re.compile(
    r'\b(?:dalam|pada|pads|sesuai|menurut|berdasarkan|antara|dengan|dari|terhadap|lihat|sebagaimana|oleh)\s*$',
    re.I
)


def _is_valid_pasal_num(tok: str) -> bool:
    if not tok or len(tok) > 4:
        return False
    if re.match(r'^\d+[A-Za-z]?$', tok):
        return True
    if re.match(r'^[IVXLCDM]+$', tok, re.I):
        return True
    if len(tok) <= 3 and all(c in '0123456789OIlLTSB' for c in tok.upper()):
        return True
    return False


def extract_pasal_items(text: str, max_items: int = 80, max_chars_per_pasal: int = 4000) -> List[dict]:
    """
    Primary: regex-based Pasal & Lampiran extractor (fast, zero API cost).
    Extracts Pasal, Lampiran, Ketentuan Tambahan/Penutup, Tables, Matrices, and Procedures.
    Handles scanned OCR character substitutions robustly across all document pages while
    preventing mid-sentence splits on inline clause/annex references.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r'[ \t]+\n', '\n', normalized)
    normalized = re.sub(r'\n{3,}', '\n\n', normalized)

    SECTION_START_RE = re.compile(
        r'(?im)^[ \t]*(?:'
        r'(?:pasal|pasa[il1]|article)[,\s]+([0-9a-z]{1,4})\b|'
        r'(?:lampiran|l[a\.\/\?]+m[p\?]+iran|i[\.\/]?[a\.\/\?]*t?m[p\?]+iran|annex|appendix)(?:[ \t]+([0-9a-z]{1,4}))?\b|'
        r'(ketentuan[ \t]+(?:tambahan|penutup|lain[-\s]?lain|peralihan))\b|'
        r'(tabel[ \t]+[^\n]{3,50})|'
        r'(matri[kx]s?[\!\s]+[^\n]{3,50})|'
        r'((?:production[ \t]+)?problem[ \t]+reportin[gq][^\n]{0,50})|'
        r'((?:service|s[ce]nice)[ \t]+(?:level|lrvel|ievel)[ \t]+agreement[^\n]{0,50})|'
        r'(formulir[ \t]+[^\n]{3,50})|'
        r'(addendum|adendum(?:[ \t]+(?:\d+|[ivxlcdm]+|[a-z]))?)\b|'
        r'(skedul|schedule(?:[ \t]+(?:\d+|[ivxlcdm]+|[a-z]))?)\b'
        r')'
    )

    raw_matches = list(SECTION_START_RE.finditer(normalized))
    matches = []
    for m in raw_matches:
        line_start = normalized.rfind('\n', 0, m.start()) + 1
        line_end = normalized.find('\n', m.start())
        if line_end == -1:
            line_end = len(normalized)
        full_line = normalized[line_start:line_end].strip()

        if len(full_line) > 95:
            continue
        if _PROSE_WORDS_RE.search(full_line):
            continue

        prev_text = normalized[max(0, line_start - 45):line_start].strip()
        if prev_text and _PREV_PREPOSITION_RE.search(prev_text):
            continue

        pasal_tok = m.group(1)
        lamp_tok = m.group(2)

        if pasal_tok is not None:
            if not _is_valid_pasal_num(pasal_tok):
                continue
            after_pasal = full_line[m.end() - line_start:].strip()
            if after_pasal and not after_pasal.startswith((':', '-', '–', '.')):
                alpha_after = [c for c in after_pasal if c.isalpha()]
                upper_ratio = sum(1 for c in alpha_after if c.isupper()) / max(len(alpha_after), 1)
                if upper_ratio < 0.6:
                    continue
        elif m.group(0).strip().lower().startswith(('lampiran', 'l.', 'l/', 'i.', 'i/', 'annex', 'appendix')):
            if lamp_tok is not None and not (_is_valid_pasal_num(lamp_tok) or lamp_tok.upper() in ('TR', 'A', 'B', 'C')):
                continue
            after_lamp = full_line[m.end() - line_start:].strip()
            if after_lamp and not after_lamp.startswith((':', '-', '–', '.')):
                alpha_line = [c for c in full_line if c.isalpha()]
                upper_ratio = sum(1 for c in alpha_line if c.isupper()) / max(len(alpha_line), 1)
                if upper_ratio < 0.65:
                    continue

        matches.append(m)

    if not matches:
        SECTION_FALLBACK_RE = re.compile(
            r'(?i)\b(?:'
            r'(?:pasal|pasa[il1]|article)[,\s]+(\d+|[ivxlcdm]+)\b|'
            r'(?:lampiran|annex|appendix)(?:\s+(\d+|[ivxlcdm]+))?\b'
            r')'
        )
        matches = list(SECTION_FALLBACK_RE.finditer(normalized))

    if len(matches) < 2:
        UPPER_HEADING_RE = re.compile(
            r'(?m)^[ \t]*(?:'
            r'(?:BAGIAN|BAB)\s+(?:[IVXLCDM]+|\d+)\b[^\n]{0,65}|'
            r'(?:[A-Z0-9]{1,3}[\.\)]\s+)?(?:'
            r'RINGKASAN\s+EKSEKUTIF|IDENTITAS\s+PARA\s+PIHAK|PARA\s+PIHAK|'
            r'RUANG\s+LINGKUP|LINGKUP\s+PEKERJAAN|OBJEK\s+KERJA\s+SAMA|'
            r'SPESIFIKASI\s+(?:ARMADA|TEKNIS|LAYANAN)|ALOKASI\s+ANGGARAN|'
            r'NILAI\s+(?:KONTRAK|PERJANJIAN|PEKERJAAN)|STRUKTUR\s+(?:BIAYA|PEMBIAYAAN)|'
            r'METODE\s+PEMBAYARAN|TATA\s+CARA\s+PEMBAYARAN|HAK\s+DAN\s+KEWAJIBAN|'
            r'JANGKA\s+WAKTU|MASA\s+BERLAKU|TINGKAT\s+LAYANAN|KOMITMEN\s+LEVEL\s+LAYANAN|'
            r'SANKSI\s+DAN\s+DENDA|DENDA\s+KETERLAMBATAN|KERAHASIAAN|'
            r'KEADAAN\s+KAHAR|FORCE\s+MAJEURE|KEKAYAAN\s+INTELEKTUAL|'
            r'KETENTUAN\s+(?:HUKUM|LAIN|PENUTUP)|PENYELESAIAN\s+SENGKETA|'
            r'PENGAKHIRAN\s+PERJANJIAN|HASIL\s+INSPEKSI|REKOMENDASI\s+TINDAK\s+LANJUT'
            r')[^\n]{0,65}'
            r')[ \t]*$'
        )
        upper_matches = [
            m for m in UPPER_HEADING_RE.finditer(normalized)
            if len(m.group(0).strip()) <= 90 and not _PROSE_WORDS_RE.search(m.group(0))
        ]
        if len(upper_matches) >= 2:
            matches = upper_matches

    pasal_items = []
    seen_labels = set()
    for idx, match in enumerate(matches):
        raw_matched = match.group(0).strip()
        lines = [l.strip() for l in normalized[match.start():match.start() + 140].splitlines() if l.strip()]
        first_line = lines[0] if lines else raw_matched
        clean_heading = first_line if len(first_line) <= 85 else raw_matched
        pasal_label = _normalize_clause_label(clean_heading)

        if pasal_label in seen_labels and "form" in pasal_label.lower():
            continue

        start_idx = match.start()
        end_idx = matches[idx + 1].start() if idx + 1 < len(matches) else len(normalized)
        section_text = normalized[start_idx:end_idx].strip()
        if len(section_text) >= 15:
            seen_labels.add(pasal_label)
            pasal_items.append({
                "pasal": pasal_label,
                "deskripsi": section_text[:max_chars_per_pasal]
            })
        if len(pasal_items) >= max_items:
            break

    return pasal_items


def llm_extract_pasal_items(full_text: str, max_items: int = 80) -> List[dict]:
    """
    Fallback LLM-based clause extractor for documents where regex fails
    (bilingual contracts, non-standard headings, Article X format, etc.).
    """
    snippet = full_text[:40000]
    prompt = (
        "Dokumen hukum berikut adalah perjanjian/kontrak atau lampiran. "
        "Ekstrak SETIAP klausul, pasal, atau bagian sebagai daftar JSON. "
        "PENTING: Salin teks lengkap setiap klausul/bagian secara utuh sesuai aslinya tanpa dipotong atau diringkas. "
        "Format: [{\"pasal\": \"Pasal 1\" (atau judul bagian), \"deskripsi\": \"teks lengkap klausul/bagian secara utuh...\"}]. "
        "Jika tidak ada heading Pasal, gunakan judul bagian atau nomor urut klausul. "
        "Kembalikan HANYA JSON array, tanpa teks lain.\n\n"
        f"DOKUMEN:\n{snippet}"
    )
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=60)
        items = extract_json(raw)
        if isinstance(items, list):
            return [{"pasal": str(i.get("pasal", f"Klausul {n+1}")),
                     "deskripsi": str(i.get("deskripsi", ""))[:4000]}
                    for n, i in enumerate(items[:max_items])
                    if i.get("deskripsi")]
    except Exception as e:
        print(f"[LLM Pasal Extractor] Failed: {e}")
    return []


def _regex_extract_date_duration(text: str) -> dict:
    """
    Regex + keyword fallback to extract document creation date and duration
    when the LLM misses them. Handles common Indonesian legal phrasings.
    """
    import re as _re
    result = {"tanggal_pembuatan": None, "durasi_perjanjian_bulan": None}

    # ── 1. Written-number month map ─────────────────────────────────────────
    BULAN_MAP = {
        "januari": 1, "februari": 2, "maret": 3, "april": 4,
        "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
        "september": 9, "oktober": 10, "november": 11, "desember": 12,
    }
    ANGKA_MAP = {
        "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
        "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10,
        "sebelas": 11, "dua belas": 12, "dua puluh empat": 24,
        "tiga puluh enam": 36, "empat puluh delapan": 48, "enam puluh": 60,
    }

    lowered = text.lower()

    # ── 2. Creation date: "hari ini [weekday] tanggal [X] [bulan] [tahun]" ──
    # Handles both digit and written day
    date_patterns = [
        # "tanggal delapan belas bulan februari tahun dua ribu dua puluh satu"
        r"tanggal\s+([\w\s]+?)\s+bulan\s+(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)\s+tahun\s+([\w\s]+)",
        # "tanggal 18 februari 2021"
        r"tanggal\s+(\d{1,2})\s+(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)\s+(\d{4})",
        # "dibuat di ... pada tanggal 18-02-2021"
        r"(?:dibuat|ditandatangani|berlaku)\s+(?:pada\s+)?tanggal\s+(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})",
        # "Jakarta, 18 Februari 2021"
        r"(?:jakarta|bandung|surabaya|medan|makassar|semarang|depok|bogor|bekasi|tangerang)[,\s]+(\d{1,2})\s+(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)\s+(\d{4})",
    ]

    WRITTEN_NUMS_DAY = {
        "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
        "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10,
        "sebelas": 11, "dua belas": 12, "tiga belas": 13, "empat belas": 14,
        "lima belas": 15, "enam belas": 16, "tujuh belas": 17, "delapan belas": 18,
        "sembilan belas": 19, "dua puluh": 20, "dua puluh satu": 21,
        "dua puluh dua": 22, "dua puluh tiga": 23, "dua puluh empat": 24,
        "dua puluh lima": 25, "dua puluh enam": 26, "dua puluh tujuh": 27,
        "dua puluh delapan": 28, "dua puluh sembilan": 29, "tiga puluh": 30,
        "tiga puluh satu": 31,
    }
    WRITTEN_YEARS = {
        "dua ribu dua puluh": 2020, "dua ribu dua puluh satu": 2021,
        "dua ribu dua puluh dua": 2022, "dua ribu dua puluh tiga": 2023,
        "dua ribu dua puluh empat": 2024, "dua ribu dua puluh lima": 2025,
        "dua ribu dua puluh enam": 2026, "dua ribu sembilan belas": 2019,
        "dua ribu delapan belas": 2018, "dua ribu tujuh belas": 2017,
    }

    for pat in date_patterns:
        m = _re.search(pat, lowered)
        if m:
            groups = m.groups()
            try:
                if len(groups) == 3:
                    g0, g1, g2 = [g.strip() for g in groups]
                    # Resolve day
                    day = int(g0) if g0.isdigit() else WRITTEN_NUMS_DAY.get(g0, None)
                    # Resolve month
                    month_str = g1.lower()
                    month = BULAN_MAP.get(month_str, None)
                    if month is None and g1.isdigit():
                        month = int(g1)
                    # Resolve year
                    year = int(g2) if g2.isdigit() else WRITTEN_YEARS.get(g2.strip(), None)
                    if day and month and year and 2000 <= year <= 2035:
                        from datetime import date
                        result["tanggal_pembuatan"] = date(year, month, day).strftime("%Y-%m-%d")
                        break
            except Exception:
                continue

    # ── 3. Duration: look inside JANGKA WAKTU section first, then globally ──
    # First, try to extract just the text of the "Jangka Waktu" clause/section
    jw_section = ""
    jw_match = _re.search(
        r'(?:pasal\s*\d+\s*)?jangka\s+waktu[\w\s]*?\n(.{0,800})',
        lowered, _re.DOTALL
    )
    if jw_match:
        jw_section = jw_match.group(0)

    search_texts = [jw_section, lowered] if jw_section else [lowered]

    dur_patterns = [
        # "minimal selama 3 (tiga) tahun"  ← from your PKS example
        r"(?:minimal|paling\s+sedikit)?\s*selama\s+(\d+)\s*(?:\([\w\s]+\))?\s*(tahun|bulan)",
        # "adalah minimal selama 3 (tiga) tahun"
        r"adalah\s+(?:minimal\s+)?selama\s+(\d+)\s*(?:\([\w\s]+\))?\s*(tahun|bulan)",
        # "berlaku selama 2 (dua) tahun"
        r"(?:berlaku|berlangsung)\s+selama\s+(\d+)\s*(?:\([\w\s]+\))?\s*(tahun|bulan)",
        # "jangka waktu ... adalah/selama 12 (dua belas) bulan"
        r"jangka\s+waktu\s+[\w\s]{0,40}?(?:adalah|selama|yaitu|:)?\s*(\d+)\s*(?:\([\w\s]+\))?\s*(tahun|bulan)",
        # "selama dua tahun" (written number)
        r"selama\s+(satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|sebelas|dua belas|dua puluh empat|tiga puluh enam)\s*(tahun|bulan)",
        # "masa berlaku ... 1 (satu) tahun"
        r"masa\s+berlaku\s+[\w\s,]{0,60}?(\d+)\s*(?:\([\w\s]+\))?\s*(tahun|bulan)",
        # "perjanjian ini berlangsung selama 24 bulan"
        r"perjanjian\s+ini\s+(?:akan\s+)?berlangsung\s+selama\s+(\d+)\s*(?:\([\w\s]+\))?\s*(tahun|bulan)",
    ]

    for search_text in search_texts:
        if result["durasi_perjanjian_bulan"]:
            break
        for pat in dur_patterns:
            m = _re.search(pat, search_text)
            if m:
                val_str, unit = m.group(1).strip(), m.group(2).strip()
                try:
                    val = int(val_str) if val_str.isdigit() else ANGKA_MAP.get(val_str, None)
                    if val:
                        result["durasi_perjanjian_bulan"] = val * 12 if unit == "tahun" else val
                        break
                except Exception:
                    continue

    return result


def understand_document(text: str) -> dict:
    """
    Pass 0A — Document Metadata Understanding.
    Focused ONLY on: doc type, parties, sector, subject.
    Date and duration are extracted by dedicated functions below.
    Uses only the preamble (first 6000 chars) for speed and accuracy.
    """
    snippet = text[:6000]
    prompt = (
        "Baca bagian awal dokumen hukum Indonesia ini dan identifikasi informasi berikut. "
        "Kembalikan HANYA JSON tanpa teks tambahan:\n"
        '{\n'
        '  "jenis_dokumen": "PKS / NDA / Perjanjian Kerja / Kontrak Layanan / dll",\n'
        '  "archetype": "PKS" | "NDA" | "DSA" | "MSA" | "EMPLOYMENT" | "PROCUREMENT" | "LEASE" | "GENERAL",\n'
        '  "pihak_pertama": "nama lengkap perusahaan/entitas pihak pertama",\n'
        '  "pihak_kedua": "nama lengkap perusahaan/entitas pihak kedua",\n'
        '  "sektor_bisnis": "teknologi / keuangan / ketenagakerjaan / perbankan / dll",\n'
        '  "pokok_perjanjian": "deskripsi singkat isi perjanjian dalam 1 kalimat"\n'
        '}\n\n'
        f"DOKUMEN (BAGIAN AWAL):\n{snippet}"
    )
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=30)
        result = extract_json(raw)
        print(f"[DocContext] Metadata: {result}")
        return result
    except Exception as e:
        print(f"[DocContext] Metadata LLM failed: {e}")
        return {
            "jenis_dokumen": "Kontrak",
            "archetype": "GENERAL",
            "pihak_pertama": "Pihak Pertama",
            "pihak_kedua": "Pihak Kedua",
            "sektor_bisnis": "umum",
            "pokok_perjanjian": "tidak teridentifikasi",
        }


def classify_document_vlm(pdf_path: str, full_text: str) -> dict:
    """Confidence-gated document classification (2026-09-30).

    One VLM pass over the rendered first page (image) plus the hybrid-
    extracted text, asking for strict JSON:
      {"klasifikasi": "Umum|Rahasia|Terbatas",
       "jenis": <active document_taxonomy name>,
       "confidence": 0-100,
       "reasoning": "<short Indonesian justification>"}

    Returns a dict with those keys plus 'kind' ('ok' | 'error'). Any failure
    (VLM unreachable, unparseable JSON, off-vocabulary labels) yields
    confidence 0 so the caller routes the doc to human pending review —
    an errored classifier can never auto-approve.
    """
    result = {"klasifikasi": "Umum", "jenis": None, "confidence": 0,
              "reasoning": "", "kind": "ok"}
    # Active taxonomy vocabulary — the same list the pending approver sees.
    try:
        tax_rows = pg_service.query(
            "SELECT name FROM document_taxonomy WHERE is_active = TRUE ORDER BY name ASC")
    except Exception as tax_err:
        tax_rows = []
        print(f"[classify] taxonomy query failed: {tax_err}")
    allowed_jenis = [r["name"] for r in tax_rows]
    if not allowed_jenis:
        result.update(reasoning="Taksonomi dokumen tidak tersedia; klasifikasi otomatis dilewati.",
                      kind="error")
        return result
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            image_b64 = vlm_extract_page_image(doc[0])
        finally:
            doc.close()
        prompt = (
            "Anda adalah analis regulasi korporat. Klasifikasikan dokumen hukum Indonesia "
            "pada gambar halaman pertama ini (teks hasil ekstraksi disertakan di bawah).\n"
            "Balas HANYA dengan objek JSON valid tanpa teks lain, berbentuk:\n"
            '{"klasifikasi": "Umum" | "Rahasia" | "Terbatas", '
            '"jenis": "<salah satu nama taksonomi di bawah>", '
            '"confidence": <integer 0-100>, '
            '"reasoning": "<1-2 kalimat bahasa Indonesia>"}\n\n'
            f"Nama taksonomi yang diperbolehkan untuk 'jenis': {', '.join(allowed_jenis)}.\n"
            "Definisi klasifikasi kerahasiaan: 'Umum' = dokumen publik/terbuka untuk semua "
            "pegawai; 'Rahasia' = berisi informasi sensitif organisasi (mis. perjanjian, "
            "data internal); 'Terbatas' = peredaran terbatas pada pihak/jabatan tertentu.\n"
            "confidence = seberapa yakin Anda pada KEDUA label sekaligus (0-100); berikan "
            "nilai rendah bila halaman pertama tidak cukup menentukan.\n\n"
            f"TEKS HASIL EKSTRAKSI (awal dokumen):\n{(full_text or '')[:2500]}"
        )
        raw = call_llm_vision(image_b64, prompt, timeout=90)
        data = extract_json(raw)
        if not isinstance(data, dict):
            raise ValueError("classifier JSON bukan objek")
        klas = str(data.get("klasifikasi", "")).strip().title()
        jenis = str(data.get("jenis", "")).strip()
        conf = int(float(data.get("confidence", 0)))
        reasoning = str(data.get("reasoning", "")).strip()[:500]
        jenis_match = next((n for n in allowed_jenis if n.lower() == jenis.lower()), None)
        if klas not in ("Umum", "Rahasia", "Terbatas"):
            result.update(reasoning=reasoning or "Label klasifikasi dari AI tidak valid.",
                          kind="error")
            return result
        if jenis_match is None:
            # Keep the secrecy recommendation for the approver, but force the
            # pending path: an off-taxonomy jenis is a failed classification.
            result.update(klasifikasi=klas, confidence=0,
                          reasoning=(f"{reasoning} Label jenis tidak ada dalam taksonomi.").strip()[:500],
                          kind="error")
            return result
        result.update(klasifikasi=klas, jenis=jenis_match,
                      confidence=max(0, min(100, conf)), reasoning=reasoning)
        print(f"[classify] {os.path.basename(pdf_path)}: {klas}/{jenis_match} conf={result['confidence']}")
        return result
    except Exception as e:
        print(f"[classify] VLM classification failed for {pdf_path}: {e}")
        result.update(reasoning=f"Klasifikasi otomatis gagal ({type(e).__name__}); diperlukan persetujuan manual.",
                      kind="error")
        return result


def extract_signing_date(text: str) -> str | None:
    """
    Pass 0B — Dedicated signing date extraction.
    Strategy:
      1. Scan preamble (first 3000 chars) where opening date is stated
      2. Scan signature block (last 2000 chars) where city+date appears
      3. Regex fallback across both windows
    """
    preamble   = text[:3000]
    sig_block  = text[-2000:]
    combined   = preamble + "\n\n[...BAGIAN AKHIR DOKUMEN...]\n\n" + sig_block

    prompt = (
        "Dari teks dokumen hukum Indonesia berikut (bagian AWAL dan AKHIR dokumen), "
        "temukan tanggal penandatanganan atau pembuatan perjanjian ini.\n"
        "Tanggal ini biasanya muncul dalam bentuk:\n"
        "- 'dibuat/ditandatangani pada hari ... tanggal [X] bulan [Y] tahun [Z]'\n"
        "- '[Kota], [tanggal] [bulan] [tahun]' (contoh: 'Jakarta, 18 Februari 2021')\n"
        "- 'tanggal delapan belas bulan februari tahun dua ribu dua puluh satu'\n"
        "Angka boleh berupa kata (delapan belas = 18, dua ribu dua puluh satu = 2021).\n"
        "Kembalikan HANYA JSON: {\"tanggal\": \"YYYY-MM-DD\"} atau {\"tanggal\": null} jika tidak ditemukan.\n\n"
        f"TEKS:\n{combined}"
    )
    llm_date = None
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=25)
        parsed = extract_json(raw)
        llm_date = parsed.get("tanggal")
        if llm_date:
            # Validate format
            datetime.strptime(llm_date, "%Y-%m-%d")
            print(f"[SigningDate] LLM found: {llm_date}")
    except Exception as e:
        print(f"[SigningDate] LLM failed or invalid date: {e}")
        llm_date = None

    if llm_date:
        return llm_date

    # Regex fallback on preamble + sig block
    regex_result = _regex_extract_date_duration(preamble + sig_block)
    if regex_result.get("tanggal_pembuatan"):
        print(f"[SigningDate] Regex fallback: {regex_result['tanggal_pembuatan']}")
        return regex_result["tanggal_pembuatan"]

    return None


def extract_duration_months(text: str) -> int | None:
    """
    Pass 0C — Dedicated duration extraction.
    Strategy:
      1. Isolate the 'JANGKA WAKTU' section using regex on section headers
      2. Feed ONLY that section (+ small buffer) to the LLM
      3. If no section found, fall back to scanning full text with LLM
      4. Regex fallback on the JANGKA WAKTU section or full text
    """

    # ── Step 1: Find and isolate the JANGKA WAKTU section ───────────────────
    # Matches: PASAL N\nJANGKA WAKTU... or just JANGKA WAKTU PELAKSANAAN...
    jw_pattern = re.compile(
        r'(?:pasal\s*\d+\s*[\n\r]+)?'
        r'jangka\s+waktu[\w\s]*?[\n\r]'
        r'(.{100,1500}?)'
        r'(?=pasal\s*\d+|\Z)',
        re.IGNORECASE | re.DOTALL
    )
    jw_match = jw_pattern.search(text)
    jw_section = jw_match.group(0) if jw_match else ""

    if jw_section:
        print(f"[Duration] Found JANGKA WAKTU section ({len(jw_section)} chars)")
        context_for_llm = jw_section[:1500]
    else:
        print("[Duration] No JANGKA WAKTU section found, using full text")
        context_for_llm = text[:20000]

    prompt = (
        "Dari teks berikut (diambil dari klausul JANGKA WAKTU perjanjian), "
        "temukan durasi/jangka waktu berlakunya perjanjian ini.\n"
        "Durasi biasanya dinyatakan dalam bentuk:\n"
        "- 'berlaku selama X tahun/bulan'\n"
        "- 'minimal selama X (Y) tahun'\n"
        "- 'jangka waktu ... adalah X bulan'\n"
        "- 'masa berlaku X tahun'\n"
        "PENTING: Konversi semua ke BULAN (1 tahun = 12 bulan, 2 tahun = 24, 3 tahun = 36).\n"
        "Kembalikan HANYA JSON: {\"durasi_bulan\": <integer>} atau {\"durasi_bulan\": null} jika tidak ditemukan.\n\n"
        f"TEKS:\n{context_for_llm}"
    )
    llm_duration = None
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=25)
        parsed = extract_json(raw)
        val = parsed.get("durasi_bulan")
        if val and isinstance(val, (int, float)) and 1 <= int(val) <= 600:
            llm_duration = int(val)
            print(f"[Duration] LLM found: {llm_duration} bulan")
    except Exception as e:
        print(f"[Duration] LLM failed: {e}")
        llm_duration = None

    if llm_duration:
        return llm_duration

    # Regex fallback — try JANGKA WAKTU section first, then full text
    for search_src in ([jw_section, text] if jw_section else [text]):
        regex_result = _regex_extract_date_duration(search_src[:30000])
        if regex_result.get("durasi_perjanjian_bulan"):
            dur = regex_result["durasi_perjanjian_bulan"]
            print(f"[Duration] Regex fallback: {dur} bulan")
            return dur

    return None

def extract_explicit_end_date(text: str) -> str | None:
    """
    Pass 0D — Extracts an explicit end date from the JANGKA WAKTU section.
    Used as a fallback when 'start_date + duration_months' fails.
    Looks for 'sampai dengan tanggal X', 'berakhir pada Y'.
    """
    jw_pattern = re.compile(
        r'(?:pasal\s*\d+\s*[\n\r]+)?'
        r'jangka\s+waktu[\w\s]*?[\n\r]'
        r'(.{100,1500}?)'
        r'(?=pasal\s*\d+|\Z)',
        re.IGNORECASE | re.DOTALL
    )
    jw_match = jw_pattern.search(text)
    context_for_llm = jw_match.group(0)[:1500] if jw_match else text[:20000]

    prompt = (
        "Dari teks berikut, temukan TANGGAL BERAKHIR (kedaluwarsa) perjanjian secara eksplisit.\n"
        "Cari frasa seperti:\n"
        "- 'berlaku ... sampai dengan tanggal [X] bulan [Y] tahun [Z]'\n"
        "- 'berakhir pada tanggal [tanggal]'\n"
        "Konversi ke format YYYY-MM-DD. Angka boleh berupa huruf (dua ribu dua puluh empat = 2024).\n"
        "Kembalikan HANYA JSON: {\"tanggal_berakhir\": \"YYYY-MM-DD\"} atau {\"tanggal_berakhir\": null} jika tidak disebutkan secara spesifik.\n\n"
        f"TEKS:\n{context_for_llm}"
    )
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=25)
        parsed = extract_json(raw)
        end_date = parsed.get("tanggal_berakhir")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
            print(f"[ExplicitEndDate] LLM found: {end_date}")
            return end_date
    except Exception as e:
        print(f"[ExplicitEndDate] LLM failed: {e}")
        
    # Regex fallback for explicit end dates
    lowered = context_for_llm.lower()
    end_date_patterns = [
        r"(?:sampai\s+dengan|berakhir\s+pada)(?:\s+tanggal)?\s+(\d{1,2})\s+(januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)\s+(\d{4})",
        r"(?:sampai\s+dengan|berakhir\s+pada)(?:\s+tanggal)?\s+(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})"
    ]
    
    BULAN_MAP = {
        "januari": 1, "februari": 2, "maret": 3, "april": 4,
        "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
        "september": 9, "oktober": 10, "november": 11, "desember": 12,
    }
    
    for pat in end_date_patterns:
        m = re.search(pat, lowered)
        if m:
            groups = m.groups()
            try:
                if len(groups) == 3:
                    day = int(groups[0])
                    month_str = groups[1].lower()
                    month = BULAN_MAP.get(month_str) if not month_str.isdigit() else int(month_str)
                    year = int(groups[2])
                    if day and month and year and 2000 <= year <= 2050:
                        from datetime import date
                        res = date(year, month, day).strftime("%Y-%m-%d")
                        print(f"[ExplicitEndDate] Regex fallback found: {res}")
                        return res
            except Exception:
                continue

    return None



def is_regulation_relevant(pasal_text: str, reg_text: str, reg_name: str) -> bool:
    """
    Relevance confirmation micro-call.
    Fast Yes/No check: does this regulation actually apply to this clause?
    Prevents wrong-domain regulations from being cited confidently.
    """
    prompt = (
        f"Apakah regulasi '{reg_name}' berikut SECARA LANGSUNG relevan untuk mengevaluasi "
        f"klausul kontrak berikut? Jawab hanya 'YA' atau 'TIDAK'.\n\n"
        f"KLAUSUL:\n{pasal_text[:400]}\n\n"
        f"REGULASI:\n{reg_text[:600]}"
    )
    try:
        ans = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=8, max_attempts=1)
        return "YA" in ans.upper()
    except Exception:
        return True  # default: assume relevant if check fails


def filter_relevant_regulations_batch(pasal_text: str, candidates: list[dict]) -> list[int]:
    """
    Single batched relevance check for up to 3 candidate regulations on a clause.
    Returns the list of 0-based indices that are directly relevant.
    """
    if not candidates:
        return []
    if len(candidates) == 1:
        c = candidates[0]
        return [0] if is_regulation_relevant(pasal_text, c["text"], c["reg_name"]) else []

    cand_block = "\n\n".join(
        f"[{i}] {c['reg_name']}:\n{c['text'][:450]}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "Pilih nomor kandidat regulasi di bawah ini yang SECARA LANGSUNG relevan untuk mengevaluasi "
        "klausul kontrak berikut. Kembalikan HANYA daftar angka dipisahkan koma (misal: 0,2), atau 'NONE' jika tidak ada.\n\n"
        f"KLAUSUL:\n{pasal_text[:400]}\n\n"
        f"KANDIDAT REGULASI:\n{cand_block}"
    )
    try:
        ans = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=10, max_attempts=1)
        up = ans.upper().strip()
        if "NONE" in up or "TIDAK" in up:
            return []
        idxs = [int(x) for x in re.findall(r"\b(\d+)\b", up)]
        return [i for i in dict.fromkeys(idxs) if 0 <= i < len(candidates)]
    except Exception:
        return list(range(len(candidates)))


_ROMAN_NUM_RE = re.compile(r'^[IVXLCDM]+$', re.I)
_COMMON_ABBR_SET = {
    'dll', 'dst', 'dkk', 'hlm', 'tgl', 'bln', 'thn', 'sdr', 'bpk', 'ny',
    'sh', 'mh', 'kn', 'spn', 'se', 'mm', 'msc', 'phd', 'drs', 'dra', 'ir',
    'rp', 'no', 'pasal', 'ayat', 'bab', 'pt', 'cv', 'ud', 'tbk', 'persero',
}
_DIGRAPH_RE = re.compile(r'(?:ng|ny|sy|kh)', re.I)
_MID_WORD_NOISE_RE = re.compile(r'[A-Za-z][*";:,\'?!{}~^\\`€&<>]+[A-Za-z]|^[?*~^€]+|[{}~^\\€]')
_WEIRD_CASE_RE = re.compile(r'^[a-z]+[A-Z]+[a-z]+')


def is_text_garbled(text: str) -> bool:
    """
    Detect font-encoding or scanner OCR corruption in extracted text.
    Calibrated for Indonesian & English legal text (collapsing digraphs ng/ny/sy/kh
    and ignoring short uppercase acronyms and Roman numerals so clean contracts
    are never falsely flagged).
    """
    if not text or len(text) < 30:
        return False

    words = text.split()
    if not words:
        return False

    VOWELS     = set("aeiouAEIOU")
    CONSONANTS = set("bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ")

    cluster_words      = 0
    no_vowel_words     = 0
    glued_words        = 0
    symbol_heavy_words = 0

    for w in words:
        alpha_str = "".join(c for c in w if c.isalpha())
        if len(alpha_str) >= 35:
            glued_words += 1

        if _MID_WORD_NOISE_RE.search(w) or _WEIRD_CASE_RE.search(w):
            symbol_heavy_words += 1

        if len(alpha_str) < 3:
            continue

        if (
            (alpha_str.isupper() and len(alpha_str) <= 5)
            or _ROMAN_NUM_RE.match(alpha_str)
            or alpha_str.lower() in _COMMON_ABBR_SET
        ):
            continue

        if not any(c in VOWELS for c in alpha_str):
            no_vowel_words += 1

        collapsed = _DIGRAPH_RE.sub('n', alpha_str)
        run = max_run = 0
        for c in collapsed:
            run = run + 1 if c in CONSONANTS else 0
            if run > max_run:
                max_run = run
        if max_run >= 4:
            cluster_words += 1

    total_words    = max(len(words), 1)
    cluster_ratio  = cluster_words  / total_words
    no_vowel_ratio = no_vowel_words / total_words
    symbol_ratio   = symbol_heavy_words / total_words

    alpha_chars = [c for c in text if c.isalpha()]
    vowel_ratio = (sum(1 for c in alpha_chars if c in VOWELS) / len(alpha_chars)
                   if alpha_chars else 0.0)

    reasons = []
    if no_vowel_ratio > 0.05:  reasons.append(f"no-vowel-words={no_vowel_ratio:.2f}")
    if cluster_ratio  > 0.06:  reasons.append(f"cluster_ratio={cluster_ratio:.2f}")
    if vowel_ratio    < 0.22:  reasons.append(f"vowel_ratio={vowel_ratio:.2f}")
    if glued_words    >= 2:     reasons.append(f"glued_words={glued_words}")
    if symbol_ratio   > 0.015: reasons.append(f"symbol_ratio={symbol_ratio:.2f}")

    garbled = bool(reasons)
    if garbled:
        print(f"  [QC] Garbled text detected: {', '.join(reasons)}")
    return garbled


def is_text_readable_llm(text: str) -> bool:
    """
    Fast LLM check to see if PyMuPDF text is readable Indonesian or garbled.
    """
    if len(text) < 50:
        return True # Too short, assume readable or handled by length check
    
    snippet = text[:500]
    prompt = (
        "Apakah teks berikut merupakan teks bahasa Indonesia/Inggris yang dapat dibaca, "
        "ataukah teks tersebut rusak/acak (garbled) karena kesalahan font encoding? "
        "Jawab HANYA dengan 'BISA DIBACA' atau 'RUSAK'.\n\n"
        f"TEKS:\n{snippet}"
    )
    try:
        ans = call_llm([{"role": "user", "content": prompt}], temperature=0.0, timeout=8, max_attempts=1)
        return "BISA DIBACA" in ans.upper()
    except Exception:
        return True # Default to readable if check fails


def extract_text_hybrid(pdf_path: str, digital_threshold: int = 80,
                        force_vlm: bool = False) -> str:
    """
    Hybrid PDF text extractor:
    - force_vlm=True : ALWAYS sends every page through VLM (used by compliance checker
                       for maximum accuracy regardless of PDF type).
    - force_vlm=False: Smart routing — clean digital pages use PyMuPDF, scanned/
                       font-corrupted pages use VLM (used by ingestion pipeline).
    """
    import fitz
    doc = fitz.open(pdf_path)
    full_text_parts = []
    vlm_pages = 0
    digital_pages = 0

    # Fast-pass LLM check on the first few pages to detect global font corruption
    global_garbled = False
    if not force_vlm:
        for p in range(min(3, len(doc))):
            pt = doc.load_page(p).get_text("text").strip()
            if len(pt) > digital_threshold:
                if is_text_garbled(pt) or not is_text_readable_llm(pt):
                    global_garbled = True
                break
        if global_garbled:
            print("  [QC] Global font corruption detected. Routing entire document to VLM.")

    import concurrent.futures

    page_tasks = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        digital_text = page.get_text("text").strip()

        use_vlm = False
        reason = ""

        if force_vlm:
            use_vlm = True
            reason = "force_vlm=True (compliance mode)"
        elif global_garbled:
            use_vlm = True
            reason = "global font corruption"
        elif len(digital_text) < digital_threshold:
            if page.get_images(full=False):
                use_vlm = True
                reason = f"scanned/empty ({len(digital_text)} chars)"
        elif is_text_garbled(digital_text):
            use_vlm = True
            reason = "font encoding corruption detected"

        img_b64 = vlm_extract_page_image(page) if use_vlm else None
        page_tasks.append((page_num, img_b64, digital_text, reason, use_vlm))

    doc.close()

    def _process_page_task(task):
        page_num, img_b64, digital_text, reason, use_vlm = task
        if not use_vlm:
            cleaned = re.sub(r'([^\n])\n([^\n])', r'\1 \2', digital_text)
            return page_num, cleaned, False

        print(f"  [VLM] Page {page_num + 1} -> {reason}")
        try:
            vlm_text = call_llm_vision(img_b64, VLM_PAGE_PROMPT, timeout=90)
            if vlm_text and vlm_text.strip():
                return page_num, f"--- Halaman {page_num + 1} ---\n{vlm_text.strip()}", True
            else:
                print(f"  [VLM] Page {page_num + 1} returned empty — keeping original.")
                return page_num, digital_text, False
        except Exception as e:
            print(f"  [VLM] Page {page_num + 1} vision error: {e} — keeping original.")
            return page_num, digital_text, False

    has_vlm = any(t[4] for t in page_tasks)
    max_workers = min(2, len(page_tasks)) if has_vlm else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_process_page_task, page_tasks))

    results.sort(key=lambda r: r[0])
    full_text_parts = []
    vlm_pages = 0
    digital_pages = 0
    for _, text, was_vlm in results:
        if text.strip():
            full_text_parts.append(text.strip())
        if was_vlm:
            vlm_pages += 1
        else:
            digital_pages += 1

    print(f"  [Hybrid] Done: {digital_pages} direct + {vlm_pages} VLM pages")
    return "\n\n".join(full_text_parts)


def _attach_filenames(contexts: List[dict]) -> None:
    """Best-effort PDF filename lookup per context, so chat sources can
       deep-link the drawer's PDF tab (critique remediation P2-5,
       2026-10-08). regulations has no filename column; the canonical
       derivation (ingest.py, reingest_missing.py, fast_ingest_txt.py) is
       os.path.basename(local_path), and /api/pdf/{filename} resolves
       exactly that basename against PDFS_DIR incl. subfolders
       (compliance.py: chunks.filename is "the local_path basename
       serve_pdf" uses). Contexts whose doc_id has no regulations row
       (internal docs, legacy orphans) simply carry filename=None."""
    doc_ids = sorted({str(c["doc_id"]) for c in contexts if c.get("doc_id")})
    if not doc_ids:
        return
    try:
        rows = pg_service.query(
            "SELECT id::text AS doc_id, local_path FROM regulations "
            "WHERE id::text = ANY(%s::text[])",
            (doc_ids,))
        fmap = {}
        for r in rows:
            lp = r.get("local_path") or ""
            fname = os.path.basename(lp.replace("\\", "/")) if lp else ""
            fmap[r["doc_id"]] = fname or None
        for c in contexts:
            c["filename"] = fmap.get(str(c.get("doc_id") or "")) or None
    except Exception as e:
        print(f"Filename lookup for chat sources failed: {e}")


def _resolve_workspace_report_ids(report_id: str | None) -> list[str]:
    """Return [report_id] plus its parent notebook_id (if any) so internal
    reference docs uploaded at the notebook level are visible in every
    document workspace belonging to that notebook."""
    if not report_id:
        return []
    ids = [report_id]
    try:
        row = pg_service.query_one(
            "SELECT notebook_id FROM compliance_history WHERE id = %s",
            (report_id,),
        )
        nb_id = row.get("notebook_id") if row else None
        if nb_id and nb_id not in ids:
            ids.append(nb_id)
    except Exception as e:
        print(f"Workspace notebook lookup failed: {e}")
    return ids


def list_workspace_references(
    user_id, report_id: str | None, excluded: set | None = None
) -> list[str]:
    """Return active (non-excluded) internal reference filenames for this workspace."""
    workspace_ids = _resolve_workspace_report_ids(report_id)
    if not workspace_ids:
        return []
    if excluded is None:
        excluded = get_user_exclusions(user_id)
    try:
        rows = pg_service.query(
            "SELECT id, doc_id, min(judul) AS judul, min(nomor) AS nomor "
            "FROM chunks "
            "WHERE visibility = 'private' AND user_id = %s::text "
            "AND report_id = ANY(%s) "
            "GROUP BY id, doc_id",
            (str(user_id), workspace_ids),
        )
    except Exception as e:
        print(f"Workspace reference list failed: {e}")
        return []

    seen: set[str] = set()
    names: list[str] = []
    for row in rows:
        if chunk_source_ref(row.get("doc_id"), row["id"]) in excluded:
            continue
        name = (row.get("judul") or row.get("nomor") or "Dokumen Internal").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _retrieve_workspace_references(
    query_vec, user_id, workspace_ids: list[str], excluded: set, limit: int = 6
) -> List[dict]:
    """Fetch top-matching active private reference chunks scoped to this workspace.

    Ensures workspace-uploaded reference documents (visibility='private',
    report_id IN workspace_ids) are always surfaced alongside public regulations,
    even when hybrid_cag short-circuits on a public law or public chunks
    outnumber private ones in the global RRF pool.
    """
    if not workspace_ids:
        return []
    try:
        rows = pg_service.query(
            "SELECT id, doc_id, text, window_context, jenis, nomor, sektor, judul "
            "FROM chunks "
            "WHERE visibility = 'private' AND user_id = %s::text "
            "AND report_id = ANY(%s) AND embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector "
            "LIMIT %s",
            (user_id, workspace_ids, query_vec, 20),
        )
    except Exception as e:
        print(f"Workspace reference retrieval failed: {e}")
        return []

    refs: List[dict] = []
    for row in rows:
        if chunk_source_ref(row.get("doc_id"), row["id"]) in excluded:
            continue
        refs.append({
            "id": row["id"],
            "doc_id": row.get("doc_id"),
            "text": _cap_context_text(row.get("window_context") or row.get("text") or ""),
            "jenis": row.get("jenis") or "Dokumen Internal",
            "nomor": row.get("nomor") or "",
            "sektor": row.get("sektor") or "Internal",
            "judul": row.get("judul") or "",
            "is_workspace_ref": True,
        })
        if len(refs) >= limit:
            break
    return refs


def _merge_workspace_refs(ws_refs: List[dict], contexts: List[dict]) -> List[dict]:
    """Prepend workspace reference contexts and deduplicate by chunk/doc id."""
    if not ws_refs:
        return contexts
    merged = list(ws_refs)
    seen_ids = {c["id"] for c in merged}
    for c in contexts:
        if c["id"] not in seen_ids:
            merged.append(c)
            seen_ids.add(c["id"])
    return merged


def retrieve_contexts(query: str, current_user: dict, n_results=5, doc_category=None, hybrid_cag=True, report_id=None, categories=None) -> List[dict]:
    """Searches Vector DB and returns structured raw dictionaries.
       If hybrid_cag=True, uses RAG to find the best document, then CAG to inject the full text.
       Enforces FR-17 by strictly filtering out classified documents the user cannot access.
       B3: report_id narrows the caller's PRIVATE internal chunks to the current
       workspace; None (no workspace) => only public law + the user's
       NULL-report_id legacy private rows, i.e. pre-B3 behavior.
    """
    report_id = report_id or None   # '' => None (no narrowing); bound as a param below
    user_id = current_user.get("id")
    role_level = auth.get_role_level(current_user.get("role", "pengguna"))
    
    # 1. Build Base Allowed Classifications
    allowed_klasifikasi = ["Umum"]
    if role_level >= 2:
        allowed_klasifikasi.append("Rahasia")
    if role_level >= 3:
        allowed_klasifikasi.append("Terbatas")
        
    # 2. Fetch Explicit Access Grants & Document Classification Map
    # (Migration M3: PG. regulations.id is int but chunks.doc_id and
    # access_grants.doc_id are TEXT -> map keys normalized with str().)
    granted_doc_ids = set()
    doc_klasifikasi_map = {}
    # B2: per-user reference-base exclusion set (empty => no-op, current behavior).
    excluded = get_user_exclusions(user_id)
    try:
        # Migration 012: only active grants count, and only 'full' grants feed
        # AI retrieval ('read' grants open the PDF but add no RAG context).
        for row in pg_service.query(
                "SELECT doc_id, permission FROM access_grants "
                "WHERE granted_to = %s::text "
                "AND " + grant_policy.active_grant_sql(),
                (user_id,)):
            if row["permission"] == "full":
                granted_doc_ids.add(row["doc_id"])
            
        for row in pg_service.query("SELECT id, klasifikasi FROM regulations"):
            doc_klasifikasi_map[str(row["id"])] = row["klasifikasi"] or "Umum"
    except Exception as e:
        print(f"Error fetching access grants for context retrieval: {e}")

    def is_allowed(reg_id: str) -> bool:
        if not reg_id:
            return True # Allow edge case for very old unstructured data
        if reg_id in granted_doc_ids:
            return True
        doc_klas = doc_klasifikasi_map.get(reg_id, "Umum")
        return doc_klas in allowed_klasifikasi

    def _touch_grant_only(ctxs):
        """Record last access for docs reachable ONLY through a grant."""
        grant_only = {
            str(c.get("doc_id")) for c in ctxs
            if str(c.get("doc_id")) in granted_doc_ids
            and doc_klasifikasi_map.get(str(c.get("doc_id")), "Umum") not in allowed_klasifikasi}
        if grant_only:
            grant_policy.touch_grants(user_id, grant_only)

    # Over-fetch for Post-Retrieval Filtering
    overfetch_n = 50
    # (Migration M3: the Chroma `where` dict becomes a SQL fragment --
    # visibility / user_id / doc_category are columns on chunks now. NULL
    # columns never match, exactly like absent Chroma metadata keys did.)
    # B3: private (internal) chunks are workspace-scoped. A NULL report_id is
    # unscoped-legacy (pre-B3) and stays visible to its owner everywhere; a
    # non-NULL report_id is narrowed to the current workspace. report_id=None
    # (no workspace) => only public + the user's NULL-report_id legacy private rows.
    workspace_ids = _resolve_workspace_report_ids(report_id)
    if workspace_ids:
        where_sql = ("(visibility = 'public' OR (user_id = %s::text "
                     "AND (report_id IS NULL OR report_id = ANY(%s))))")
        where_params = [user_id, workspace_ids]
    else:
        where_sql = ("(visibility = 'public' OR (user_id = %s::text "
                     "AND report_id IS NULL))")
        where_params = [user_id]
    if doc_category:
        where_sql += " AND doc_category = ANY(%s)"
        where_params.append([doc_category, "UMUM"])
    where_sql += " AND COALESCE(is_active_law, TRUE) = TRUE"
    if categories:
        cat_list = list(categories)
        if "CIVIL_CONTRACT" not in cat_list:
            cat_list.append("CIVIL_CONTRACT")
        where_sql += " AND (visibility = 'private' OR category_code = ANY(%s))"
        where_params.append(cat_list)
        
    contexts = []
    query_vec = embed_query(query)
    ws_refs = _retrieve_workspace_references(query_vec, user_id, workspace_ids, excluded)

    if hybrid_cag:
        # 1. RAG Discovery: Find the single most relevant chunk
        # (Migration M3: pgvector cosine `<=>` with the M2-verified HNSW
        # search width.)
        with pg_service.get_conn() as conn:
            conn.execute("SET LOCAL hnsw.ef_search = 100")
            # (2026-09-29 RAG audit: with the global hnsw.iterative_scan=off,
            # the filtered ANN scan exhausted its ef candidates before the
            # LIMIT -- a LIMIT 50 returned only 41 rows. strict_order keeps
            # exact distance ordering while continuing the index scan until
            # the LIMIT is met. Requires pgvector >= 0.8; stack is 0.8.6.)
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            cur = conn.execute(
                "SELECT id, doc_id, jenis, nomor, sektor, judul "
                "FROM chunks "
                f"WHERE embedding IS NOT NULL AND {where_sql} "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                tuple(where_params) + (query_vec, overfetch_n))
            discovery_rows = cur.fetchall()
        
        best_reg_id = None
        best_top_meta = None
        
        if discovery_rows:
            # Post-Filter to find the top AUTHORIZED document
            for meta in discovery_rows:
                reg_id = meta["doc_id"]
                
                # B2: an excluded source is skipped like an unauthorized one, so
                # the CAG whole-document winner can never be a deselected doc.
                if is_allowed(reg_id) and chunk_source_ref(reg_id, meta["id"]) not in excluded:
                    best_reg_id = reg_id
                    best_top_meta = meta
                    break # Found the highest ranked authorized document
            
            if best_reg_id:
                # 2. CAG Injection: Fetch all chunks for this specific document
                # (Migration M3: ORDER BY seq reproduces the legacy Chroma
                # insertion order -- 004_chunks_seq.sql assigned seq in the
                # original collection order.)
                # (2026-09-29 RAG audit: SUM-guard in SQL -- pulling
                # multi-megabyte docs (the corpus holds an 8.8 MB single
                # chunk) into Python just to discover they exceed the CAG
                # budget wasted serious work whenever such a doc won
                # discovery. Over-budget docs now return zero rows and fall
                # through; the exact len(full_text) check below remains the
                # authoritative gate.)
                doc_results = pg_service.query(
                    "SELECT text FROM chunks WHERE doc_id = %s "
                    "AND (SELECT COALESCE(SUM(LENGTH(text)), 0) FROM chunks "
                    "     WHERE doc_id = %s) <= 25000 "
                    "ORDER BY seq",
                    (best_reg_id, best_reg_id))
                
                if doc_results:
                    # Reconstruct full text
                    full_text = "\n".join(r["text"] for r in doc_results)
                    
                    # Safeguard: Limit to ~25,000 characters to avoid exceeding context window
                    if len(full_text) <= 25000:
                        contexts.append({
                            "id": best_reg_id,
                            "doc_id": best_reg_id,
                            "text": full_text,
                            "jenis": best_top_meta["jenis"] or '',
                            "nomor": best_top_meta["nomor"] or '',
                            "sektor": best_top_meta["sektor"] or '',
                            "judul": best_top_meta["judul"] or ''
                        })
                        contexts = _merge_workspace_refs(ws_refs, contexts)
                        _attach_filenames(contexts)
                        _touch_grant_only(contexts)
                        return contexts
                    # If it exceeds 25,000 chars, we DO NOT return contexts here. 
                    # We let it fall through to the standard chunk-based RAG below so the LLM gets the most relevant snippets instead of just the first 25k chars.
    # Fallback to Hybrid Retrieval (Dense + BM25 Sparse) & RRF
    chunk_data = {}
    dense_ranks = {}
    sparse_ranks = {}
    
    # --- 1. Dense Retrieval (pgvector HNSW, cosine `<=>`) ---
    with pg_service.get_conn() as conn:
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")  # see discovery pass above
        cur = conn.execute(
            "SELECT id, doc_id, text, window_context, jenis, nomor, sektor, judul "
            "FROM chunks "
            f"WHERE embedding IS NOT NULL AND {where_sql} "
            "ORDER BY embedding <=> %s::vector "
            "LIMIT %s",
            tuple(where_params) + (query_vec, overfetch_n))
        dense_rows = cur.fetchall()
    
    dense_rank = 1
    if dense_rows:
        for meta in dense_rows:
            reg_id = meta["doc_id"]
            
            if not is_allowed(reg_id):
                continue
            if chunk_source_ref(reg_id, meta["id"]) in excluded:  # B2
                continue
                
            chunk_id = meta["id"]
            window_doc = _cap_context_text(meta["window_context"] if meta["window_context"] else meta["text"])
            
            dense_ranks[chunk_id] = dense_rank
            chunk_data[chunk_id] = {
                "id": chunk_id,
                "doc_id": meta["doc_id"],
                "text": window_doc,
                "jenis": meta["jenis"] or '',
                "nomor": meta["nomor"] or '',
                "sektor": meta["sektor"] or '',
                "judul": meta["judul"] or ''
            }
            dense_rank += 1

    # --- 2. Sparse Retrieval (PG tsvector, full corpus) ---
    # (Migration M3: replaces SQLite FTS5 BM25 on chunks_fts. The " OR "-joined
    # word string feeds websearch_to_tsquery, which parses the OR operator
    # exactly like the old MATCH syntax and never raises on stray punctuation.
    # 2026-09-29 RAG audit: the sparse_legacy=TRUE gate is gone. It confined
    # BM25 to the 1,269-row migrated FTS corpus (2% of chunks) even though
    # content_tsv is populated on 100% of rows, so the "hybrid" half was
    # effectively dead weight and its hits were the small legacy docs. On
    # Indonesian legal text the lexical side is exactly what catches precise
    # terms (nomor, pasal, istilah) that the English-centric MiniLM embedder
    # misses. sparse_legacy remains as a lineage marker only.)
    # Clean query for FTS syntax to avoid errors
    safe_query = query.replace('"', '').replace("'", "")
    safe_query = " OR ".join([word for word in safe_query.split() if len(word) > 2])
    
    try:
        # We fetch extra because we still need to filter by is_allowed
        sparse_rows = pg_service.query(
            "SELECT c.id, c.doc_id, c.text, c.window_context, "
            "r.judul, r.nomor, r.jenis, r.sektor "
            "FROM chunks c "
            "JOIN regulations r ON r.id::text = c.doc_id "
            "WHERE c.content_tsv @@ websearch_to_tsquery('simple', %s) "
            "ORDER BY ts_rank_cd(c.content_tsv, websearch_to_tsquery('simple', %s)) DESC "
            "LIMIT %s",
            (safe_query, safe_query, overfetch_n * 2))
        
        sparse_rank = 1
        for row in sparse_rows:
            chunk_id, doc_id = row["id"], row["doc_id"]
            if not is_allowed(doc_id):
                continue
            if chunk_source_ref(doc_id, chunk_id) in excluded:  # B2
                continue
                
            sparse_ranks[chunk_id] = sparse_rank
            
            # If the dense pass didn't find this chunk, we need to populate its data
            # (2026-09-29 RAG audit: metadata now rides the JOIN above -- the
            # old per-row regulations query_one was an N+1 through an
            # unindexable id::text cast, and the INNER JOIN preserves the old
            # drop-orphan-chunks behavior since orphans have no regulations row.)
            if chunk_id not in chunk_data:
                chunk_data[chunk_id] = {
                    "id": chunk_id,
                    "doc_id": doc_id,
                    "text": _cap_context_text(row["window_context"] or row["text"]),
                    "jenis": row["jenis"] or '',
                    "nomor": row["nomor"] or '',
                    "sektor": row["sektor"] or '',
                    "judul": row["judul"] or ''
                }
            sparse_rank += 1
    except Exception as e:
        print(f"Sparse Retrieval Error: {e}")

    # --- 3. Reciprocal Rank Fusion (RRF) ---
    k = 60
    rrf_scores = {}
    
    unique_chunk_ids = set(dense_ranks.keys()).union(set(sparse_ranks.keys()))
    for cid in unique_chunk_ids:
        score = 0.0
        if cid in dense_ranks:
            score += 1.0 / (k + dense_ranks[cid])
        if cid in sparse_ranks:
            score += 1.0 / (k + sparse_ranks[cid])
        rrf_scores[cid] = score
        
    # Sort by descending RRF score, take top 15 for Reranking
    ranked_chunks = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:15]
    
    # Extract candidate dictionaries
    candidates = []
    for cid, score in ranked_chunks:
        if cid in chunk_data:
            candidates.append(chunk_data[cid])
            
    # --- 4. Cross-Encoder Reranking ---
    if candidates:
        try:
            reranker = get_reranker()
            # Prepare pairs: (query, text)
            # Use the base text or window_context for scoring
            pairs = [[query, cand["text"]] for cand in candidates]
            
            # Predict scores
            scores = reranker.predict(pairs)
            
            # Attach scores to candidates
            for idx, score in enumerate(scores):
                candidates[idx]["rerank_score"] = float(score)
                
            # Sort candidates by rerank_score descending
            candidates = sorted(candidates, key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            print(f"Reranking failed, falling back to RRF sort: {e}")
            
    # Build final context list
    for cand in candidates[:n_results]:
        contexts.append(cand)

    contexts = _merge_workspace_refs(ws_refs, contexts)
    _attach_filenames(contexts)
    _touch_grant_only(contexts)
    return contexts
