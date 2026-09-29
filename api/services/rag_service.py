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
from typing import List
from datetime import datetime
import auth
from services import pg_service
from services.embed_service import embed_query
from services.llm_client import call_glm, call_glm_vision, vlm_extract_page_image, VLM_PAGE_PROMPT, extract_json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Migration M3: get_chroma_collection() removed -- the vector store is the
# PG `chunks` table now. Former callers (repository.py, knowledge_graph.py,
# and retrieve_contexts below) query PG directly via services.pg_service.

_cross_encoder = None

def get_reranker():
    global _cross_encoder
    if _cross_encoder is None:
        import os
        from sentence_transformers import CrossEncoder
        model_name = os.environ.get("RERANKER_MODEL_NAME", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        print(f"Initializing CrossEncoder reranker: {model_name}")
        _cross_encoder = CrossEncoder(model_name, max_length=512)
    return _cross_encoder

def extract_pasal_items(text: str, max_items: int = 20, max_chars_per_pasal: int = 900) -> List[dict]:
    """
    Primary: regex-based Pasal extractor (fast, zero API cost).
    Used as the first attempt. LLM-based extractor is used as fallback in check_compliance.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r'[ \t]+\n', '\n', normalized)
    normalized = re.sub(r'\n{3,}', '\n\n', normalized)

    pasal_matches = list(re.finditer(r'(?im)^\s*pasal\s+((?:\d+|[ivxlcdm]+))\b', normalized))
    if not pasal_matches:
        pasal_matches = list(re.finditer(r'(?i)\bpasal\s+((?:\d+|[ivxlcdm]+))\b', normalized))

    pasal_items = []
    for idx, match in enumerate(pasal_matches):
        pasal_no_raw = match.group(1).upper()
        pasal_label = f"Pasal {pasal_no_raw}"
        start_idx = match.start()
        end_idx = pasal_matches[idx + 1].start() if idx + 1 < len(pasal_matches) else len(normalized)
        pasal_text = normalized[start_idx:end_idx].strip()
        if len(pasal_text) >= 20:
            pasal_items.append({
                "pasal": pasal_label,
                "deskripsi": pasal_text[:max_chars_per_pasal]
            })
        if len(pasal_items) >= max_items:
            break

    return pasal_items


def llm_extract_pasal_items(full_text: str, max_items: int = 20) -> List[dict]:
    """
    Fallback LLM-based clause extractor for documents where regex fails
    (bilingual contracts, non-standard headings, Article X format, etc.).
    """
    snippet = full_text[:8000]
    prompt = (
        "Dokumen hukum berikut adalah perjanjian/kontrak. "
        "Ekstrak SETIAP klausul atau pasal sebagai daftar JSON. "
        "Format: [{\"pasal\": \"Pasal 1\", \"deskripsi\": \"teks lengkap klausul...\"}]. "
        "Jika tidak ada heading Pasal, gunakan nomor urut klausul. "
        "Kembalikan HANYA JSON array, tanpa teks lain.\n\n"
        f"DOKUMEN:\n{snippet}"
    )
    try:
        raw = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=60)
        items = extract_json(raw)
        if isinstance(items, list):
            return [{"pasal": str(i.get("pasal", f"Klausul {n+1}")),
                     "deskripsi": str(i.get("deskripsi", ""))[:900]}
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
        '  "pihak_pertama": "nama lengkap perusahaan/entitas pihak pertama",\n'
        '  "pihak_kedua": "nama lengkap perusahaan/entitas pihak kedua",\n'
        '  "sektor_bisnis": "teknologi / keuangan / ketenagakerjaan / perbankan / dll",\n'
        '  "pokok_perjanjian": "deskripsi singkat isi perjanjian dalam 1 kalimat"\n'
        '}\n\n'
        f"DOKUMEN (BAGIAN AWAL):\n{snippet}"
    )
    try:
        raw = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=30)
        result = extract_json(raw)
        print(f"[DocContext] Metadata: {result}")
        return result
    except Exception as e:
        print(f"[DocContext] Metadata LLM failed: {e}")
        return {
            "jenis_dokumen": "Kontrak",
            "pihak_pertama": "Pihak Pertama",
            "pihak_kedua": "Pihak Kedua",
            "sektor_bisnis": "umum",
            "pokok_perjanjian": "tidak teridentifikasi",
        }


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
        raw = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=25)
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
        raw = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=25)
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
        raw = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=25)
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
        ans = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=20)
        return "YA" in ans.upper()
    except Exception:
        return True  # default: assume relevant if check fails


def is_text_garbled(text: str) -> bool:
    """
    Detect font-encoding corruption in PyMuPDF-extracted text.
    Returns True if the text looks garbled/unreadable.

    Three independent signals — triggering ANY ONE marks the page as corrupted:
    1. Words with ZERO vowels (length >= 3): e.g. 'KBWJBN', 'PHRBN'
       These are statistically impossible in Indonesian/English legal text.
    2. Consonant clusters of 3+ in >= 8% of words: e.g. 'KBWAJIBAN' (K-B-W = 3)
       Previous threshold was 4, which missed these.
    3. Global vowel ratio < 20%: whole-page signal for systematic encoding failure.
    """
    if len(text) < 30:
        return False

    words = text.split()
    if not words:
        return False

    VOWELS    = set("aeiouAEIOU")
    CONSONANTS = set("bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ")

    cluster_words  = 0   # words with 3+ consecutive consonants
    no_vowel_words = 0   # words with NO vowels at all (len >= 3)

    for w in words:
        alpha = [c for c in w if c.isalpha()]
        if not alpha:
            continue

        # Signal 1: zero-vowel words (strong indicator of garbling)
        if len(alpha) >= 3 and not any(c in VOWELS for c in alpha):
            no_vowel_words += 1

        # Signal 2: consonant cluster (3+ threshold, down from 4)
        if len(alpha) >= 3:
            run = max_run = 0
            for c in alpha:
                run = run + 1 if c in CONSONANTS else 0
                max_run = max(max_run, run)
            if max_run >= 3:
                cluster_words += 1

    total_words    = max(len(words), 1)
    cluster_ratio  = cluster_words  / total_words
    no_vowel_ratio = no_vowel_words / total_words

    # Signal 3: global vowel ratio
    alpha_chars = [c for c in text if c.isalpha()]
    vowel_ratio = (sum(1 for c in alpha_chars if c in VOWELS) / len(alpha_chars)
                   if alpha_chars else 0.0)

    reasons = []
    if no_vowel_ratio > 0.05:  reasons.append(f"no-vowel-words={no_vowel_ratio:.2f}")
    if cluster_ratio  > 0.08:  reasons.append(f"cluster_ratio={cluster_ratio:.2f}")
    if vowel_ratio    < 0.20:  reasons.append(f"vowel_ratio={vowel_ratio:.2f}")

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
        ans = call_glm([{"role": "user", "content": prompt}], temperature=0.0, timeout=20)
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
            use_vlm = True
            reason = f"scanned/empty ({len(digital_text)} chars)"
        elif is_text_garbled(digital_text):
            use_vlm = True
            reason = "font encoding corruption detected"

        if not use_vlm:
            cleaned = re.sub(r'([^\n])\n([^\n])', r'\1 \2', digital_text)
            full_text_parts.append(cleaned)
            digital_pages += 1
        else:
            print(f"  [VLM] Page {page_num + 1} -> {reason}")
            try:
                img_b64 = vlm_extract_page_image(page)
                vlm_text = call_glm_vision(img_b64, VLM_PAGE_PROMPT, timeout=60)
                if vlm_text.strip():
                    full_text_parts.append(vlm_text.strip())
                    vlm_pages += 1
                else:
                    print(f"  [VLM] Page {page_num + 1} returned empty — keeping original.")
                    full_text_parts.append(digital_text)
            except Exception as e:
                print(f"  [VLM] Page {page_num + 1} vision error: {e} — keeping original.")
                full_text_parts.append(digital_text)

    doc.close()
    print(f"  [Hybrid] Done: {digital_pages} direct + {vlm_pages} VLM pages")
    return "\n\n".join(full_text_parts)


def retrieve_contexts(query: str, current_user: dict, n_results=5, doc_category=None, hybrid_cag=True) -> List[dict]:
    """Searches Vector DB and returns structured raw dictionaries.
       If hybrid_cag=True, uses RAG to find the best document, then CAG to inject the full text.
       Enforces FR-17 by strictly filtering out classified documents the user cannot access.
    """
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
    try:
        for row in pg_service.query(
                "SELECT doc_id FROM access_grants "
                "WHERE granted_to = %s::text "
                "AND (expires_at IS NULL OR expires_at >= NOW())",
                (user_id,)):
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

    # Over-fetch for Post-Retrieval Filtering
    overfetch_n = 50
    # (Migration M3: the Chroma `where` dict becomes a SQL fragment --
    # visibility / user_id / doc_category are columns on chunks now. NULL
    # columns never match, exactly like absent Chroma metadata keys did.)
    where_sql = "(visibility = 'public' OR user_id = %s::text)"
    where_params = [user_id]
    if doc_category:
        where_sql += " AND doc_category = ANY(%s)"
        where_params.append([doc_category, "UMUM"])
        
    contexts = []
    
    if hybrid_cag:
        # 1. RAG Discovery: Find the single most relevant chunk
        # (Migration M3: pgvector cosine `<=>` with the M2-verified HNSW
        # search width.)
        query_vec = embed_query(query)
        with pg_service.get_conn() as conn:
            conn.execute("SET LOCAL hnsw.ef_search = 100")
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
                
                if is_allowed(reg_id):
                    best_reg_id = reg_id
                    best_top_meta = meta
                    break # Found the highest ranked authorized document
            
            if best_reg_id:
                # 2. CAG Injection: Fetch all chunks for this specific document
                # (Migration M3: ORDER BY seq reproduces the legacy Chroma
                # insertion order -- 004_chunks_seq.sql assigned seq in the
                # original collection order.)
                doc_results = pg_service.query(
                    "SELECT text FROM chunks WHERE doc_id = %s ORDER BY seq",
                    (best_reg_id,))
                
                if doc_results:
                    # Reconstruct full text
                    full_text = "\n".join(r["text"] for r in doc_results)
                    
                    # Safeguard: Limit to ~25,000 characters to avoid exceeding context window
                    if len(full_text) <= 25000:
                        contexts.append({
                            "id": best_reg_id,
                            "text": full_text,
                            "jenis": best_top_meta["jenis"] or '',
                            "nomor": best_top_meta["nomor"] or '',
                            "sektor": best_top_meta["sektor"] or '',
                            "judul": best_top_meta["judul"] or ''
                        })
                        return contexts
                    # If it exceeds 25,000 chars, we DO NOT return contexts here. 
                    # We let it fall through to the standard chunk-based RAG below so the LLM gets the most relevant snippets instead of just the first 25k chars.
    # Fallback to Hybrid Retrieval (Dense + BM25 Sparse) & RRF
    chunk_data = {}
    dense_ranks = {}
    sparse_ranks = {}
    
    # --- 1. Dense Retrieval (pgvector HNSW, cosine `<=>`) ---
    # (query_vec was computed for the CAG discovery pass above; recompute
    # only when hybrid_cag is disabled.)
    if not hybrid_cag:
        query_vec = embed_query(query)
    with pg_service.get_conn() as conn:
        conn.execute("SET LOCAL hnsw.ef_search = 100")
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
                
            chunk_id = meta["id"]
            window_doc = meta["window_context"] if meta["window_context"] else meta["text"]
            
            dense_ranks[chunk_id] = dense_rank
            chunk_data[chunk_id] = {
                "id": chunk_id,
                "text": window_doc,
                "jenis": meta["jenis"] or '',
                "nomor": meta["nomor"] or '',
                "sektor": meta["sektor"] or '',
                "judul": meta["judul"] or ''
            }
            dense_rank += 1

    # --- 2. Sparse Retrieval (PG tsvector over legacy FTS rows) ---
    # (Migration M3: replaces SQLite FTS5 BM25 on chunks_fts. The " OR "-joined
    # word string feeds websearch_to_tsquery, which parses the OR operator
    # exactly like the old MATCH syntax and never raises on stray punctuation.
    # Only sparse_legacy=TRUE rows -- the migrated chunks_fts corpus -- take
    # part, matching legacy index membership.)
    # Clean query for FTS syntax to avoid errors
    safe_query = query.replace('"', '').replace("'", "")
    safe_query = " OR ".join([word for word in safe_query.split() if len(word) > 2])
    
    try:
        # We fetch extra because we still need to filter by is_allowed
        sparse_rows = pg_service.query(
            "SELECT id, doc_id, text, window_context "
            "FROM chunks "
            "WHERE sparse_legacy "
            "AND content_tsv @@ websearch_to_tsquery('simple', %s) "
            "ORDER BY ts_rank_cd(content_tsv, websearch_to_tsquery('simple', %s)) DESC "
            "LIMIT %s",
            (safe_query, safe_query, overfetch_n * 2))
        
        sparse_rank = 1
        for row in sparse_rows:
            chunk_id, doc_id = row["id"], row["doc_id"]
            text, window_context = row["text"], row["window_context"]
            if not is_allowed(doc_id):
                continue
                
            sparse_ranks[chunk_id] = sparse_rank
            
            # If the dense pass didn't find this chunk, we need to populate its data
            if chunk_id not in chunk_data:
                # To get metadata like 'judul', we query the main table
                # (chunks.doc_id is TEXT; regulations.id is int -> ::text)
                reg_row = pg_service.query_one(
                    "SELECT judul, nomor, jenis, sektor FROM regulations "
                    "WHERE id::text = %s", (doc_id,))
                if reg_row:
                    judul, nomor, jenis, sektor = (reg_row["judul"], reg_row["nomor"],
                                                   reg_row["jenis"], reg_row["sektor"])
                    chunk_data[chunk_id] = {
                        "id": chunk_id,
                        "text": window_context if window_context else text,
                        "jenis": jenis or '',
                        "nomor": nomor or '',
                        "sektor": sektor or '',
                        "judul": judul or ''
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
        
    return contexts
