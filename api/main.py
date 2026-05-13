import os
import sys
import re
import json
import time
import requests
import chromadb
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
import auth
from typing import List, Optional, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import shutil
import hashlib
import uuid
import base64
import logging
from dotenv import load_dotenv
import concurrent.futures
from datetime import datetime
from dateutil.relativedelta import relativedelta

# ── File logging setup ──────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Redirect all print() calls to the logger so nothing is missed
import builtins as _builtins
_real_print = _builtins.print
def _log_print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    logging.info(msg)
_builtins.print = _log_print


# ── Load environment ────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CHROMA_DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

# ── GLM API Config ──────────────────────────────────────────────────────────
API_BASE_URL = os.getenv("MODEL_BASE_URL", "https://console.labahasa.ai/v1").rstrip("/")
API_KEY      = os.getenv("MODEL_API_KEY", "")
# Reverted to use Maverick for both text and vision because GLM is unstable
GLM_MODEL    = os.getenv("LLAMA_MODEL", "llama-4-maverick-instruct")
VLM_MODEL    = os.getenv("LLAMA_MODEL", "llama-4-maverick-instruct")
GLM_MAX_ATTEMPTS = int(os.getenv("GLM_MAX_ATTEMPTS", "5"))
GLM_RETRY_BACKOFF_BASE = float(os.getenv("GLM_RETRY_BACKOFF_BASE", "0.8"))

if not API_BASE_URL or not API_KEY:
    print("[WARNING] MODEL_BASE_URL or MODEL_API_KEY is not set in .env — LLM calls will fail.")

# Setup Paths for Parser
sys.path.append(os.path.join(BASE_DIR, "parser"))
from pdf_parser import LegalDocumentParser, LegalChunker

# Setup FastAPI App
app = FastAPI(title="Legal Analyzer API")

# ── Init DB for metadata ────────────────────────────────────────────────────
def init_main_db():
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS regulations
                 (id TEXT PRIMARY KEY, judul TEXT, pdf_url TEXT, 
                  nomor TEXT, jenis TEXT, sektor TEXT, 
                  status TEXT, detail_url TEXT, local_path TEXT)''')
    try:
        c.execute("ALTER TABLE regulations ADD COLUMN klasifikasi TEXT DEFAULT 'Publik'")
    except sqlite3.OperationalError:
        pass
        
    c.execute('''CREATE TABLE IF NOT EXISTS access_grants (
        id TEXT PRIMARY KEY,
        doc_id TEXT,
        granted_by TEXT,
        granted_to TEXT,
        reason TEXT,
        expires_at TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_main_db()

import auth
import history
import internal_docs
import templates

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(history.router, prefix="/api", tags=["history"])
app.include_router(internal_docs.router, prefix="/api", tags=["internal_docs"])
app.include_router(templates.router, prefix="/api", tags=["templates"])

# Serve local PDFs as static files
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")
os.makedirs(PDFS_DIR, exist_ok=True)
# NOTE: We do NOT use app.mount(StaticFiles) because Starlette mounts bypass CORS middleware.
# Instead we use a regular route below (/api/pdf/{filename}) which correctly gets CORS headers.

# Setup CORS to allow frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For prototype purposes
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
class ChatRequest(BaseModel):
    messages: List[dict]  # [{"role": "user", "content": "..."}, ...]

class Source(BaseModel):
    id: str
    jenis: str
    nomor: str
    sektor: str
    judul: str
    snippet: str

class ChatResponse(BaseModel):
    answer: str
    sources: List[Source]

class AnalyzeRequest(BaseModel):
    reg_id: Optional[str] = None
    nomor: str
    judul: str
    filename: Optional[str] = None

    @field_validator('reg_id', 'nomor', 'judul', mode='before')
    @classmethod
    def coerce_to_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)

class AccessGrantRequest(BaseModel):
    granted_to: str
    reason: str
    expires_at: Optional[str] = None

# ChromaDB Cache
_collection = None
_glm_session: Optional[requests.Session] = None

GENERIC_GLM_ERROR_MESSAGE = (
    "Layanan AI sedang mengalami gangguan koneksi sementara. "
    "Silakan coba lagi dalam beberapa saat."
)

def get_chroma_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
        _collection = client.get_or_create_collection(
            name="ojk_regulations",
            metadata={"hnsw:space": "cosine"}
        )
    return _collection

def get_glm_session() -> requests.Session:
    """Build a reusable HTTP session with transport-level retries."""
    global _glm_session
    if _glm_session is None:
        session = requests.Session()
        retry_cfg = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_cfg, pool_connections=10, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _glm_session = session
    return _glm_session

def is_transient_network_error(err: Exception | None) -> bool:
    if err is None:
        return False
    text = str(err).lower()
    transient_markers = [
        "name resolution",
        "failed to resolve",
        "getaddrinfo failed",
        "remote end closed connection",
        "connection aborted",
        "connection reset",
        "temporarily unavailable",
        "timed out",
        "timeout",
    ]
    return any(marker in text for marker in transient_markers)

def extract_pasal_items(text: str, max_items: int = 20, max_chars_per_pasal: int = 900) -> List[dict]:
    """
    Extract Pasal blocks from document text.
    Supports both Arabic and Roman numerals, e.g., "Pasal 3" and "PASAL III".
    """
    # Normalize line endings and spacing around line breaks for stable regex matching.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r'[ \t]+\n', '\n', normalized)
    normalized = re.sub(r'\n{3,}', '\n\n', normalized)

    # Strict heading match first (line-based)
    pasal_matches = list(re.finditer(r'(?im)^\s*pasal\s+((?:\d+|[ivxlcdm]+))\b', normalized))
    # Fallback for documents where line breaks are collapsed by extractor
    if not pasal_matches:
        pasal_matches = list(re.finditer(r'(?i)\bpasal\s+((?:\d+|[ivxlcdm]+))\b', normalized))

    pasal_items = []
    for idx, match in enumerate(pasal_matches):
        pasal_no_raw = match.group(1).upper()
        pasal_label = f"Pasal {pasal_no_raw}"
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
        raw = re.sub(r'```json|```', '', raw).strip()
        items = json.loads(raw)
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
        raw = re.sub(r'```json|```', '', raw).strip()
        result = json.loads(raw)
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
        raw = re.sub(r'```json|```', '', raw).strip()
        parsed = json.loads(raw)
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
    lowered = text.lower()

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
        raw = re.sub(r'```json|```', '', raw).strip()
        parsed = json.loads(raw)
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
        raw = re.sub(r'```json|```', '', raw).strip()
        parsed = json.loads(raw)
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

def vlm_extract_page_image(page) -> str:
    """
    Render a PyMuPDF page as a PNG image and return it as a base64-encoded string.
    Uses 288 DPI (3x scale) to ensure clear distinction of characters like 'j' and 'l'.
    """
    import fitz
    mat = fitz.Matrix(3, 3)  # 3x = ~288 DPI
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("png")).decode("utf-8")


def call_glm_vision(image_b64: str, prompt: str, timeout: int = 90) -> str:
    """
    Send a page image to llama-4-maverick via the vision (multimodal) API.
    Returns the extracted text content.
    """
    if API_BASE_URL.endswith("/chat/completions"):
        url = API_BASE_URL
    else:
        url = f"{API_BASE_URL}/chat/completions"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": VLM_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"}
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }],
        "temperature": 0.0,
        "stream": False
    }
    resp = get_glm_session().post(url, json=payload, headers=headers, timeout=(15, timeout))
    if resp.ok:
        return resp.json()["choices"][0]["message"]["content"]
    print(f"VLM page extraction failed: {resp.status_code} {resp.text[:200]}")
    return ""


VLM_PAGE_PROMPT = (
    "Ekstrak SEMUA teks dari dokumen ini secara akurat. "
    "Perbaiki kesalahan ejaan visual/typo hasil OCR (misalnya huruf 'l' yang seharusnya 'J') "
    "agar membentuk kalimat bahasa Indonesia yang baku dan masuk akal. "
    "Pertahankan struktur asli seperti nomor pasal dan ayat. "
    "Jangan tambahkan penjelasan atau komentar di luar teks dokumen."
)



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


def call_glm(messages: list, temperature: float = 0.1, timeout: int = 90) -> str:
    """
    Unified GLM API caller (OpenAI-compatible).
    Returns the assistant message content string.
    Raises HTTPException on failure.
    """
    # Handle cases where user might have included /chat/completions in the base URL
    if API_BASE_URL.endswith("/chat/completions"):
        url = API_BASE_URL
    else:
        url = f"{API_BASE_URL}/chat/completions"
    
    # DEBUG: See what the server is actually using
    print(f"DEBUG: Calling LLM at URL: {url}")
    print(f"DEBUG: Model: {GLM_MODEL}")

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False
    }
    max_attempts = max(1, GLM_MAX_ATTEMPTS)
    last_error: Exception | None = None
    session = get_glm_session()

    for attempt in range(1, max_attempts + 1):
        try:
            # Tuple timeout: (connect_timeout, read_timeout)
            resp = session.post(url, json=payload, headers=headers, timeout=(15, timeout))

            if not resp.ok:
                # Include truncated upstream body to speed up debugging bad credentials/model/payload.
                body_snippet = (resp.text or "")[:300]
                print(f"GLM non-2xx response: status={resp.status_code}, body={body_snippet}")
                raise HTTPException(
                    status_code=500,
                    detail=GENERIC_GLM_ERROR_MESSAGE
                )

            return resp.json()["choices"][0]["message"]["content"]
        except HTTPException:
            raise
        except requests.exceptions.RequestException as e:
            last_error = e
            print(f"GLM request attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                # Exponential backoff to absorb transient upstream disconnects.
                sleep_s = GLM_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                time.sleep(sleep_s)
                continue
            break
        except (KeyError, IndexError, TypeError) as e:
            print(f"Unexpected GLM response format: {e}")
            raise HTTPException(status_code=500, detail=GENERIC_GLM_ERROR_MESSAGE)

    if is_transient_network_error(last_error):
        raise HTTPException(status_code=503, detail=GENERIC_GLM_ERROR_MESSAGE)
    raise HTTPException(status_code=500, detail=GENERIC_GLM_ERROR_MESSAGE)


def retrieve_contexts(query: str, current_user: dict, n_results=5, doc_category=None, hybrid_cag=True) -> List[dict]:
    """Searches Vector DB and returns structured raw dictionaries.
       If hybrid_cag=True, uses RAG to find the best document, then CAG to inject the full text.
       Enforces FR-17 by strictly filtering out classified documents the user cannot access.
    """
    collection = get_chroma_collection()
    user_id = current_user.get("id")
    role_level = auth.get_role_level(current_user.get("role", "pengguna"))
    
    # 1. Build Base Allowed Classifications
    allowed_klasifikasi = ["Publik"]
    if role_level >= 2:
        allowed_klasifikasi.append("Rahasia")
    if role_level >= 3:
        allowed_klasifikasi.append("Terbatas")
        
    # 2. Fetch Explicit Access Grants & Document Classification Map
    granted_doc_ids = set()
    doc_klasifikasi_map = {}
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT doc_id FROM access_grants WHERE granted_to = ? AND expires_at >= datetime('now')", (user_id,))
        for row in c.fetchall():
            granted_doc_ids.add(row[0])
            
        c.execute("SELECT id, klasifikasi FROM regulations")
        for row in c.fetchall():
            doc_klasifikasi_map[row[0]] = row[1] or "Publik"
        conn.close()
    except Exception as e:
        print(f"Error fetching access grants for context retrieval: {e}")

    def is_allowed(reg_id: str) -> bool:
        if not reg_id:
            return True # Allow edge case for very old unstructured data
        if reg_id in granted_doc_ids:
            return True
        doc_klas = doc_klasifikasi_map.get(reg_id, "Publik")
        return doc_klas in allowed_klasifikasi

    # Over-fetch for Post-Retrieval Filtering
    overfetch_n = 50
    where_clause = {"$or": [{"visibility": "public"}, {"user_id": user_id}]}
    if doc_category:
        where_clause = {
            "$and": [
                where_clause,
                {"doc_category": {"$in": [doc_category, "UMUM"]}}
            ]
        }
        
    contexts = []
    
    if hybrid_cag:
        # 1. RAG Discovery: Find the single most relevant chunk
        discovery = collection.query(query_texts=[query], n_results=overfetch_n, where=where_clause)
        
        best_reg_id = None
        best_top_meta = None
        
        if discovery and discovery['documents'] and len(discovery['documents'][0]) > 0:
            # Post-Filter to find the top AUTHORIZED document
            for idx in range(len(discovery['documents'][0])):
                meta = discovery['metadatas'][0][idx]
                reg_id = meta.get("reg_id")
                
                if is_allowed(reg_id):
                    best_reg_id = reg_id
                    best_top_meta = meta
                    break # Found the highest ranked authorized document
            
            if best_reg_id:
                # 2. CAG Injection: Fetch all chunks for this specific document
                doc_results = collection.get(where={"reg_id": best_reg_id}, include=["documents"])
                
                if doc_results and doc_results['documents']:
                    # Reconstruct full text
                    full_text = "\n".join(doc_results['documents'])
                    
                    # Safeguard: Limit to ~25,000 characters to avoid exceeding context window
                    if len(full_text) <= 25000:
                        contexts.append({
                            "id": best_reg_id,
                            "text": full_text,
                            "jenis": best_top_meta.get('jenis', ''),
                            "nomor": best_top_meta.get('nomor', ''),
                            "sektor": best_top_meta.get('sektor', ''),
                            "judul": best_top_meta.get('judul', '')
                        })
                        return contexts
                    # If it exceeds 25,000 chars, we DO NOT return contexts here. 
                    # We let it fall through to the standard chunk-based RAG below so the LLM gets the most relevant snippets instead of just the first 25k chars.
                    
    # Fallback to standard chunk-based RAG
    results = collection.query(
        query_texts=[query],
        n_results=overfetch_n,
        where=where_clause
    )
    
    if results and results['documents']:
        for i in range(len(results['documents'][0])):
            meta = results['metadatas'][0][i]
            reg_id = meta.get("reg_id")
            
            # Apply Security Filter (FR-17)
            if not is_allowed(reg_id):
                continue
                
            doc = results['documents'][0][i]
            id_str = results['ids'][0][i]
            
            contexts.append({
                "id": id_str,
                "text": doc,
                "jenis": meta.get('jenis', ''),
                "nomor": meta.get('nomor', ''),
                "sektor": meta.get('sektor', ''),
                "judul": meta.get('judul', '')
            })
            
            # Stop once we have enough authorized chunks
            if len(contexts) >= n_results:
                break
            
    return contexts

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest, current_user: dict = Depends(auth.get_current_user)):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages array cannot be empty")
        
    last_user_message = next((m["content"] for m in reversed(req.messages) if m["role"] == "user"), None)
    if not last_user_message:
        raise HTTPException(status_code=400, detail="Missing user message")

    # 1. Retrieve Context
    contexts = retrieve_contexts(last_user_message, current_user=current_user)
    
    # Format context for the prompt
    context_str = ""
    sources_to_return = []
    for i, c in enumerate(contexts):
        sources_to_return.append(Source(
            id=c['id'],
            jenis=c['jenis'],
            nomor=c['nomor'],
            sektor=c['sektor'],
            judul=c['judul'],
            snippet=c['text']
        ))
        context_str += f"SUMBER [{i+1}]: {c['jenis']} Nomor {c['nomor']}\nTEKS:\n{c['text']}\n\n"

    # Fetch user's contract monitor stats
    user_stats_str = ""
    try:
        conn = auth.get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM compliance_history WHERE user_id = ?", (current_user["id"],))
        rows = c.fetchall()
        total_docs = len(rows)
        conn.close()
        user_stats_str = f"INFO SISTEM (DASHBOARD PENGGUNA): Pengguna saat ini memiliki total {total_docs} dokumen yang tersimpan dan dipantau di dalam Contract Monitor.\n\n"
    except Exception as e:
        print(f"Failed to fetch user stats for chatbot: {e}")

    system_prompt = (
        "Anda adalah pakar hukum teknologi dan penasihat kontrak di Indonesia (Fokus: UU ITE, POJK, KUHPerdata, UU PDP, UU HAM). "
        "Gunakan HANYA konteks hukum yang diberikan untuk menjawab pertanyaan pengguna.\n\n"
        f"{user_stats_str}"
        "PANDUAN KETAT (WAJIB DIIKUTI):\n"
        "1. ZERO META-LANGUAGE: Jangan pernah menggunakan frasa seperti 'Berdasarkan konteks yang diberikan', 'Meskipun konteks tidak menyebutkan', atau 'Menurut konteks hukum'. Anggap fakta hukum sebagai pengetahuan bawaan Anda. Jawab langsung dengan percaya diri layaknya penasihat hukum sungguhan.\n"
        "2. FORMAT PERCAKAPAN & MUDAH DIBACA: Jangan gunakan judul kaku seperti 'Analisis Hukum' atau 'Kesimpulan'. Gunakan alur percakapan yang alami, empatik, dan profesional. Awali jawaban secara langsung dengan 'Ya', 'Tidak', atau 'Tergantung'. Gunakan paragraf pendek, teks tebal (bold) untuk istilah kunci, dan poin-poin agar mudah dibaca di layar ponsel.\n"
        "3. ARGUMEN HUKUM KRITIS:\n"
        "   - Jika ditanya tentang kontrak elektronik vs kertas bermeterai: Tegaskan bahwa meterai hanyalah pajak dokumen, BUKAN syarat sahnya perjanjian. Sahnya perjanjian murni didasarkan pada Pasal 1320 KUHPerdata dan Pasal 5 UU ITE.\n"
        "   - Jika ditanya tentang ancaman penjara untuk utang: Anda WAJIB mengutip 'Pasal 19 ayat (2) UU No. 39 Tahun 1999 tentang Hak Asasi Manusia (UU HAM)' yang melarang keras hukuman pidana/penjara untuk masalah utang piutang perdata. Bedakan dengan jelas antara gagal bayar perdata (wanprestasi) dan niat jahat (penipuan, Pasal 378 KUHP).\n\n"
        "Jawablah dalam Bahasa Indonesia yang profesional dan menenangkan, memberikan solusi yang dapat ditindaklanjuti, dan selalu mengutip dasar hukum/pasal yang relevan secara natural."
    )

    enriched_user_prompt = f"PERTANYAAN PENGGUNA:\n{last_user_message}\n\nKONTEKS HUKUM:\n{context_str}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": enriched_user_prompt}
    ]
    try:
        answer = call_glm(messages, temperature=0.1, timeout=60)
    except HTTPException as e:
        # Upstream LLM can intermittently disconnect; keep chat endpoint stable for frontend UX.
        print(f"Chat fallback triggered due to GLM error: {e.detail}")
        answer = (
            "Maaf, layanan AI sedang mengalami gangguan koneksi sementara. "
            "Silakan coba kirim pertanyaan yang sama beberapa detik lagi."
        )
    return ChatResponse(answer=answer, sources=sources_to_return)

@app.post("/api/chat-session")
async def save_chat_session(req: ChatRequest, current_user: dict = Depends(auth.get_current_user)):
    """Save an entire chat session history (called after chat_endpoint or independently)"""
    # For a full implementation, the frontend would pass a session ID, or we generate one.
    # To keep it simple, we will just use a session_id logic here.
    pass # Implementation details added below...


@app.get("/api/pdf/{filename}")
async def serve_pdf(filename: str):
    """
    Return PDF bytes encoded as base64 JSON.
    IDM cannot intercept this because the Content-Type is application/json, not application/pdf.
    The frontend decodes the base64 and creates a blob:// URL to render in an iframe.
    """
    import base64
    safe_filename = os.path.basename(filename)  # prevent path traversal
    file_path = os.path.join(PDFS_DIR, safe_filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"PDF '{safe_filename}' not found")
    with open(file_path, "rb") as f:
        pdf_bytes = f.read()
    return {"filename": safe_filename, "data": base64.b64encode(pdf_bytes).decode("utf-8")}

@app.get("/api/repository")
async def get_repository(current_user: dict = Depends(auth.get_current_user)):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    if not os.path.exists(db_path):
        return {"documents": []}
        
    user_id = current_user.get("id")
    role_level = auth.get_role_level(current_user.get("role", "pengguna"))
    
    allowed_klasifikasi = ["Publik"]
    if role_level >= 2:
        allowed_klasifikasi.append("Rahasia")
    if role_level >= 3:
        allowed_klasifikasi.append("Terbatas")
        
    klas_str = ", ".join(f"'{k}'" for k in allowed_klasifikasi)
        
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # Query regulations where klasifikasi is allowed OR explicitly granted
    query = f"""
        SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi 
        FROM regulations 
        WHERE local_path IS NOT NULL AND local_path != ''
        AND (
            klasifikasi IN ({klas_str}) 
            OR id IN (
                SELECT doc_id FROM access_grants 
                WHERE granted_to = ? 
                AND (expires_at IS NULL OR expires_at = '' OR expires_at >= datetime('now'))
            )
        )
    """
    c.execute(query, (user_id,))
    records = c.fetchall()
    
    docs = []
    for row in records:
        # Depending on schema, klasifikasi might be at index 7. Handle safely.
        reg_id, judul, nomor, jenis, sektor, status, local_path = row[:7]
        klasifikasi = row[7] if len(row) > 7 else "Publik"
        filename = os.path.basename(local_path) if local_path else None
        docs.append({
            "id": str(reg_id) if reg_id is not None else None,
            "judul": str(judul) if judul else "",
            "nomor": str(nomor) if nomor is not None else "",
            "jenis": str(jenis) if jenis else "",
            "sektor": str(sektor) if sektor else "",
            "status": str(status) if status else "",
            "klasifikasi": str(klasifikasi) if klasifikasi else "Publik",
            "filename": filename
        })
    conn.close()
    
    return {"documents": docs}

@app.post("/api/documents/{doc_id}/grant-access")
async def grant_document_access(
    doc_id: str,
    req: AccessGrantRequest,
    current_user: dict = Depends(auth.get_current_user)
):
    user_level = auth.get_role_level(current_user.get("role", "pengguna"))
    if user_level < 2:
        raise HTTPException(status_code=403, detail="Hanya Manajer atau Direktur yang dapat memberikan akses.")
        
    import sqlite3
    import uuid
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    c.execute("SELECT klasifikasi FROM regulations WHERE id = ?", (doc_id,))
    doc = c.fetchone()
    if not doc:
        conn.close()
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
        
    klasifikasi = doc[0] if len(doc) > 0 and doc[0] else "Publik"
    
    if klasifikasi == "Terbatas" and user_level < 3:
        conn.close()
        raise HTTPException(status_code=403, detail="Manajer tidak dapat memberikan akses untuk dokumen Terbatas.")
        
    if not req.reason or len(req.reason.strip()) < 5:
        conn.close()
        raise HTTPException(status_code=400, detail="Alasan wajib diisi.")
        
    grant_id = str(uuid.uuid4())
    c.execute('''INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason, expires_at)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (grant_id, doc_id, current_user["id"], req.granted_to, req.reason, req.expires_at))
    conn.commit()
    conn.close()
    
    return {"message": "Akses berhasil diberikan."}

@app.post("/api/upload")
async def upload_document(
    file: UploadFile = File(...), 
    doc_type: str = Form("regulations"),
    klasifikasi: str = Form("Publik"),
    current_user: dict = Depends(auth.require_role("manajer"))
):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    import sqlite3
    import uuid
    from datetime import datetime
    
    # Setup directories
    pdfs_dir = os.path.join(BASE_DIR, "data", "pdfs")
    os.makedirs(pdfs_dir, exist_ok=True)
    
    file_path = os.path.join(pdfs_dir, file.filename)
    
    # Save physical file temporarily
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        parser = LegalDocumentParser()
        chunker = LegalChunker()
        
        full_text = parser.parse_pdf(file_path)
        
        # ── Duplicate Detection (FR-5) ──────────────────────────────────────
        fingerprint_text = full_text[:1500]
        collection = get_chroma_collection()
        dup_results = collection.query(
            query_texts=[fingerprint_text],
            n_results=1
        )
        
        if dup_results and dup_results['distances'] and len(dup_results['distances'][0]) > 0:
            dist = dup_results['distances'][0][0]
            if dist < 0.15:
                # Cleanup temp file
                if os.path.exists(file_path):
                    os.remove(file_path)
                duplicate_judul = dup_results['metadatas'][0][0].get('judul', 'Dokumen Tidak Diketahui')
                raise HTTPException(status_code=400, detail=f"Duplikat Terdeteksi: Dokumen ini sangat mirip dengan '{duplicate_judul}'.")

        # ── Proceed with Saving ─────────────────────────────────────────────
        db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS regulations
                     (id TEXT PRIMARY KEY, judul TEXT, pdf_url TEXT, 
                      nomor TEXT, jenis TEXT, sektor TEXT, 
                      status TEXT, detail_url TEXT, local_path TEXT)''')
                      
        # Generate ID and Metadata
        doc_id = str(uuid.uuid4())[:8]
        judul = file.filename.replace('.pdf', '')
        nomor = f"CUSTOM-{doc_id}"
        
        if doc_type == "internal":
            jenis = "Dokumen Internal"
            sektor = "Dokumen Internal"
        else:
            jenis = "Regulasi Custom"
            sektor = "Upload Manual"
        status = "Berlaku"
        
        c.execute('''INSERT OR REPLACE INTO regulations 
                     (id, judul, pdf_url, nomor, jenis, sektor, status, detail_url, local_path, klasifikasi) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (doc_id, judul, "", nomor, jenis, sektor, status, "", file_path, klasifikasi))
        conn.commit()
        conn.close()
        
        print(f"File saved to DB. Now parsing and embedding: {file.filename}")
        
        # Vector DB Injection
        base_metadata = {
            "reg_id": doc_id,
            "judul": judul,
            "nomor": nomor,
            "jenis": jenis,
            "sektor": sektor,
            "status": status
        }
        
        chunks = chunker.chunk_document(full_text, base_metadata)
        
        documents = []
        metadatas = []
        ids = []
        
        for i, c_data in enumerate(chunks):
            clean_metadata = {k: v for k, v in c_data["metadata"].items() if v is not None}
            chunk_id = f"{nomor}_chunk_{i}"
            hash_id = hashlib.md5(chunk_id.encode('utf-8')).hexdigest()
            
            documents.append(c_data["text"])
            metadatas.append(clean_metadata)
            ids.append(hash_id)
            
        if documents:
            collection = get_chroma_collection()
            collection.add(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            print(f"Successfully added {len(documents)} chunks to ChromaDB!")
        
        return {"status": "success", "message": f"Successfully processed {len(documents)} logic chunks into knowledge base."}
        
    except Exception as e:
        print(f"Error processing PDF: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process PDF text: {str(e)}")

@app.post("/api/analyze")
async def analyze_document(req: AnalyzeRequest):
    """Analyzes a document from ChromaDB chunks + LLM for overview and status."""
    collection = get_chroma_collection()
    
    # Step 1: Fetch all chunks for this specific document
    # Prefer reg_id (unique) but fall back to nomor if not available
    try:
        if req.reg_id:
            results = collection.get(
                where={"reg_id": req.reg_id},
                include=["documents"]
            )
        else:
            results = collection.get(
                where={"nomor": req.nomor},
                include=["documents"]
            )
        chunks = results.get("documents", [])
    except Exception as e:
        print(f"ChromaDB fetch error: {e}")
        chunks = []
    
    if not chunks:
        return {
            "total_pasal": 0,
            "overview": "Dokumen ini belum memiliki data di knowledge base.",
            "status": {"dicabut": [], "diubah_dengan": []},
            "outline": []
        }
    
    # Step 2: Build outline + find max Pasal number (more accurate than chunk count)
    import re
    outline = []
    seen = set()
    max_pasal_num = 0
    
    for chunk in chunks:
        lines = chunk.strip().split('\n')
        first_line = lines[0].strip() if lines else ""
        
        # Detect BAB headers
        if re.match(r'^BAB\s+[IVXLC\d]+', first_line, re.IGNORECASE):
            entry = first_line[:80]
            if entry not in seen:
                outline.append({"type": "bab", "text": entry})
                seen.add(entry)
        
        # Detect Pasal headers — track the max number found
        pasal_match = re.search(r'Pasal\s+(\d+)', chunk, re.IGNORECASE)
        if pasal_match:
            num = int(pasal_match.group(1))
            if num > max_pasal_num:
                max_pasal_num = num
            
            entry = first_line[:60]
            if re.match(r'^Pasal\s+\d+', first_line, re.IGNORECASE) and entry not in seen:
                outline.append({"type": "pasal", "text": entry})
                seen.add(entry)

    # Step 3: Take sample text for LLM (first 6 chunks ≈ 3500 chars)
    sample_text = "\n---\n".join(chunks[:6])[:3500]
    
    system_prompt = (
        "Anda adalah analis hukum OJK Indonesia. Berikan analisis singkat dan akurat dalam format JSON yang ketat. "
        "Jangan menambahkan teks di luar JSON."
    )
    
    user_prompt = (
        f"Analisis dokumen hukum berikut:\n"
        f"Judul: {req.judul}\nNomor: {req.nomor}\n\n"
        f"TEKS DOKUMEN:\n{sample_text}\n\n"
        f"Kembalikan HANYA JSON berikut (tanpa markdown, tanpa kode blok):\n"
        '{"overview": "ringkasan 2-3 kalimat", '
        '"dicabut": ["nama peraturan yang dicabut jika ada, atau array kosong"], '
        '"diubah_dengan": ["nama peraturan pengubah jika ada, atau array kosong"]}'
    )
    
    messages_llm = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_prompt}
    ]
    
    overview = "Tidak dapat mengambil ringkasan dari dokumen ini."
    dicabut = []
    diubah_dengan = []
    
    try:
        raw_content = call_glm(messages_llm, temperature=0.1, timeout=60)
        raw_content = re.sub(r'```json|```', '', raw_content).strip()
        parsed = json.loads(raw_content)
        overview = parsed.get("overview", overview)
        dicabut = parsed.get("dicabut", [])
        diubah_dengan = parsed.get("diubah_dengan", [])
    except HTTPException:
        raise
    except Exception as e:
        print(f"LLM analysis error: {e}")
    
    return {
        "total_pasal": max_pasal_num,
        "overview": overview,
        "status": {"dicabut": dicabut, "diubah_dengan": diubah_dengan},
        "outline": outline
    }

@app.post("/api/analyze-pasals")
async def analyze_pasals(req: AnalyzeRequest):
    """
    Deep analysis: uses LLM to analyze each Pasal individually,
    producing status, perbandingan (comparison), and hubungan (hierarchy).
    """
    collection = get_chroma_collection()

    try:
        if req.reg_id:
            results = collection.get(where={"reg_id": req.reg_id}, include=["documents"])
        else:
            results = collection.get(where={"nomor": req.nomor}, include=["documents"])
        chunks = results.get("documents", [])
    except Exception as e:
        print(f"ChromaDB error: {e}")
        chunks = []

    if not chunks:
        return {"pasals": [], "error": "Tidak ada data untuk dokumen ini."}

    # ── Build pasal map: {pasal_num (int): text (str)} ──
    pasal_texts: dict[int, str] = {}
    for chunk in chunks:
        m = re.search(r'Pasal\s+(\d+)', chunk, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if num not in pasal_texts:
                pasal_texts[num] = chunk[:500]

    sorted_pasals = sorted(pasal_texts.items())[:15]   # limit to 15 most relevant pasals

    if not sorted_pasals:
        return {"pasals": [], "error": "Tidak dapat mengekstrak Pasal dari dokumen ini."}

    # ── Build LLM prompt ──
    doc_text = ""
    for num, text in sorted_pasals:
        doc_text += f"\nPasal {num}:\n{text[:380]}\n---\n"

    system_prompt = (
        "Anda adalah analis regulasi di Indonesia yang sangat teliti dan akurat. "
        "Selalu jawab dalam Bahasa Indonesia. Jangan menambahkan teks di luar JSON."
    )
    user_prompt = (
        f"Analisis regulasi berikut secara mendalam:\n"
        f"Judul: {req.judul}\nNomor: {req.nomor}\n\n"
        f"TEKS REGULASI:\n{doc_text}\n\n"
        f"Untuk SETIAP Pasal di atas, berikan analisis dalam format JSON strict berikut "
        f"(kembalikan HANYA array JSON, tanpa teks tambahan):\n"
        '[{"pasal":"Pasal 1","status":"Tidak Berubah",'
        '"isi":"ringkasan isi pasal dalam 1-2 kalimat",'
        '"perbandingan":"perbandingan konten pasal ini dengan regulasi sebelumnya yang dicabut atau diubah",'
        '"hubungan":"hubungan dengan Undang-Undang atau Peraturan Pemerintah yang lebih tinggi"},'
        '{"pasal":"Pasal 2",...}]'
        '\n\nNilai status yang valid: "Tidak Berubah", "Diubah", "Baru", "Dicabut".'
    )

    try:
        raw = call_glm([
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ], temperature=0.1, timeout=180)
        raw = re.sub(r'```json|```', '', raw).strip()

        # The LLM might return an object {"pasals":[...]} OR a bare array [...]
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            items = parsed.get("pasals", parsed.get("data", []))
        elif isinstance(parsed, list):
            items = parsed
        else:
            items = []

        # Attach the actual extracted pasal text for the frontend to display
        pasal_map = {num: text for num, text in sorted_pasals}
        for item in items:
            m = re.search(r'\d+', item.get("pasal", ""))
            if m:
                item["isi_teks"] = pasal_map.get(int(m.group()), "")[:350]

        return {"pasals": items}

    except json.JSONDecodeError as e:
        print(f"JSON parse error in analyze-pasals: {e}\nRaw: {raw[:300]}")
        return {"pasals": [], "error": "LLM memberikan respons yang tidak valid. Coba lagi."}
    except Exception as e:
        print(f"Deep analysis error: {e}")
        return {"pasals": [], "error": str(e)}


def process_single_pasal(c, doc_type, pihak_1, pihak_2, sektor, pokok, collection, COSINE_THRESHOLD):
    pasal_label = c.get("pasal", "Pasal")
    desc = c.get("deskripsi", "")
    if not desc or len(desc) < 10:
        return None

    # Map doc_type to doc_category
    doc_cat = "NDA" if "NDA" in str(doc_type).upper() or "NON-DISCLOSURE" in str(doc_type).upper() else "PKS"

    # Enrich the search query with document context for better RAG retrieval
    search_query = f"[{doc_type}] [{sektor}] {desc[:400]}"
    sr = collection.query(
        query_texts=[search_query], 
        n_results=3,
        where={"doc_category": {"$in": [doc_cat, "UMUM"]}}
    )

    supporting_regulations = []
    relevant_refs = []

    if sr and sr.get('documents') and len(sr['documents'][0]) > 0:
        for i in range(len(sr['documents'][0])):
            dist = sr['distances'][0][i]
            if dist < COSINE_THRESHOLD:
                doc_text = sr['documents'][0][i]
                meta = sr['metadatas'][0][i]
                reg_name = f"{meta.get('jenis', 'Aturan')} Nomor {meta.get('nomor', 'N/A')}"

                # Relevance confirmation micro-call
                if is_regulation_relevant(desc, doc_text, reg_name):
                    supporting_regulations.append({
                        "jenis": meta.get('jenis', 'Aturan'),
                        "nomor": meta.get('nomor', 'N/A'),
                        "sektor": meta.get('sektor', 'Umum'),
                        "teks": doc_text
                    })
                    relevant_refs.append(f"--- {reg_name} ---\n{doc_text[:700]}")
                else:
                    print(f"  [Relevance] {reg_name} rejected for {pasal_label}")

    if not relevant_refs:
        return {
            "pasal": pasal_label,
            "isi_pasal": desc[:500],
            "status": "TIDAK DIATUR",
            "penjelasan": "Tidak ditemukan regulasi yang secara langsung relevan dengan klausul ini dalam database saat ini.",
            "rekomendasi": "Pastikan klausul ini wajar secara keperdataan dan tidak bertentangan dengan KUH Perdata.",
            "ai_analysis": "",
            "supporting_regulations": supporting_regulations
        }

    combined_regs = "\n\n".join(relevant_refs)
    prompt_verify = (
        "Anda adalah auditor kepatuhan hukum senior Indonesia yang sangat teliti. "
        "Analisis klausul kontrak berikut secara mendalam menggunakan regulasi yang tersedia.\n\n"

        f"KONTEKS DOKUMEN:\n"
        f"- Jenis: {doc_type}\n"
        f"- Pihak Pertama: {pihak_1}\n"
        f"- Pihak Kedua: {pihak_2}\n"
        f"- Sektor: {sektor}\n"
        f"- Pokok Perjanjian: {pokok}\n\n"

        f"KLAUSUL YANG DIANALISIS ({pasal_label}):\n{desc}\n\n"

        f"REGULASI YANG BERLAKU:\n{combined_regs}\n\n"

        "INSTRUKSI ANALISIS (ikuti urutan ini):\n"
        "1. Identifikasi: Apa topik hukum utama klausul ini? (pembayaran, kerahasiaan, PHK, dll)\n"
        "2. Kesesuaian: Apakah klausul ini SECARA EKSPLISIT diatur, dilarang, atau diwajibkan oleh regulasi di atas?\n"
        "   Kutip pasal/ayat spesifik dari regulasi jika ada.\n"
        "3. Risiko: Identifikasi risiko hukum konkret bagi pihak yang lebih lemah.\n"
        "4. Verdict: Berikan satu dari tiga status:\n"
        "   - SESUAI: klausul patuh dan tidak berisiko\n"
        "   - BERESIKO: klausul perlu perbaikan atau klarifikasi\n"
        "   - FATAL: klausul melanggar ketentuan wajib regulasi\n\n"
        "Kembalikan HANYA JSON berikut (tanpa markdown, tanpa teks lain):\n"
        '{\n'
        '  "status": "SESUAI" | "BERESIKO" | "FATAL",\n'
        '  "pasal_regulasi_terkait": "misal: Pasal 15 ayat (3) PP 45/2015",\n'
        '  "penjelasan": "penjelasan spesifik 1-2 kalimat merujuk regulasi",\n'
        '  "rekomendasi": "saran perbaikan konkret (kosongkan jika SESUAI)",\n'
        '  "ai_analysis": "dampak bisnis jangka panjang (isi hanya jika BERESIKO atau FATAL)"\n'
        '}'
    )

    try:
        raw_v = call_glm(
            [{"role": "user", "content": prompt_verify}],
            temperature=0.1,
            timeout=90
        )
        raw_v = re.sub(r'```json|```', '', raw_v).strip()
        ans = json.loads(raw_v)

        status_raw = str(ans.get("status", "BERESIKO")).upper()
        if "SESUAI" in status_raw:       status_final = "SESUAI"
        elif "FATAL" in status_raw:       status_final = "FATAL"
        elif "TIDAK DIATUR" in status_raw: status_final = "TIDAK DIATUR"
        else:                              status_final = "BERESIKO"

        return {
            "pasal": pasal_label,
            "isi_pasal": desc[:500],
            "status": status_final,
            "pasal_regulasi_terkait": ans.get("pasal_regulasi_terkait", ""),
            "penjelasan": ans.get("penjelasan", "Gagal diverifikasi"),
            "rekomendasi": ans.get("rekomendasi", ""),
            "ai_analysis": ans.get("ai_analysis", ""),
            "supporting_regulations": supporting_regulations
        }

    except HTTPException as e:
        print(f"[Compliance] Verification HTTP error: {e.detail}")
        return {
            "pasal": pasal_label, "isi_pasal": desc[:500], "status": "ERROR",
            "pasal_regulasi_terkait": "",
            "penjelasan": "Layanan AI sedang mengalami gangguan koneksi sementara. Silakan coba lagi dalam beberapa saat.",
            "rekomendasi": "Coba ulang analisis.",
            "ai_analysis": "", "supporting_regulations": supporting_regulations
        }
    except Exception as e:
        print(f"[Compliance] Parsing error for {pasal_label}: {e}")
        return {
            "pasal": pasal_label, "isi_pasal": desc[:500], "status": "ERROR",
            "pasal_regulasi_terkait": "",
            "penjelasan": "Format respons LLM tidak valid.",
            "rekomendasi": "-",
            "ai_analysis": "", "supporting_regulations": supporting_regulations
        }

def extract_text_multi_format(file_path: str, filename: str) -> str:
    """
    Extracts text from various file formats: PDF, DOCX, XLSX, PPTX, JPG, PNG, TXT.
    Routes to the appropriate extractor based on the file extension.
    """
    ext = filename.lower().split('.')[-1]
    
    if ext == 'pdf':
        return extract_text_hybrid(file_path, force_vlm=False)
        
    elif ext == 'txt':
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
            
    elif ext in ['jpg', 'jpeg', 'png']:
        print(f"  [VLM] Image extraction for {filename}")
        with open(file_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')
        vlm_text = call_glm_vision(img_b64, VLM_PAGE_PROMPT, timeout=60)
        return vlm_text.strip()
        
    elif ext == 'docx':
        try:
            import docx
            doc = docx.Document(file_path)
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text.strip())
            for table in doc.tables:
                for row in table.rows:
                    row_data = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_data:
                        full_text.append(" | ".join(row_data))
            return "\n".join(full_text)
        except Exception as e:
            print(f"Error parsing DOCX: {e}")
            return ""
            
    elif ext == 'pptx':
        try:
            from pptx import Presentation
            prs = Presentation(file_path)
            full_text = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        full_text.append(shape.text.strip())
            return "\n".join(full_text)
        except Exception as e:
            print(f"Error parsing PPTX: {e}")
            return ""
            
    elif ext == 'xlsx':
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, data_only=True)
            ws = wb.active
            full_text = []
            for row in ws.iter_rows(values_only=True):
                row_data = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if row_data:
                    full_text.append(" | ".join(row_data))
            return "\n".join(full_text)
        except Exception as e:
            print(f"Error parsing XLSX: {e}")
            return ""
            
    else:
        return ""


@app.post("/api/check-compliance")
async def check_compliance(file: UploadFile = File(...)):
    """
    Accepts a document upload (PDF, DOCX, XLSX, PPTX, JPG, PNG, TXT), extracts text, 
    and uses a multi-step LLM pipeline to cross-check clauses against the OJK repository.
    """
    allowed_exts = ['.pdf', '.docx', '.xlsx', '.pptx', '.jpg', '.jpeg', '.png', '.txt']
    ext = os.path.splitext(file.filename.lower())[1]
    
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Format file tidak didukung. Harap unggah PDF, DOCX, XLSX, PPTX, JPG, PNG, atau TXT.")
        
    upload_id = str(uuid.uuid4())[:8]
    temp_path = os.path.join(PDFS_DIR, f"temp_check_{upload_id}_{file.filename}")
    
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        # ── Pass 0: Multi-Format Extraction ──────────────────────────────────
        print(f"[Compliance] Starting extraction for: {file.filename}")
        full_text = extract_text_multi_format(temp_path, file.filename)
        text_snippet = full_text[:35000]

        if len(text_snippet.strip()) < 100:
            raise HTTPException(
                status_code=400,
                detail="Dokumen tidak dapat dibaca. Coba unggah ulang atau pastikan PDF tidak terenkripsi."
            )

        # ── Pass 0A: Document Metadata Understanding ────────────────────────
        print("[Compliance] Pass 0A: Extracting document metadata...")
        doc_ctx = understand_document(full_text)
        doc_type = doc_ctx.get("jenis_dokumen", "Kontrak")
        pihak_1  = doc_ctx.get("pihak_pertama", "Pihak Pertama")
        pihak_2  = doc_ctx.get("pihak_kedua", "Pihak Kedua")
        sektor   = doc_ctx.get("sektor_bisnis", "umum")
        pokok    = doc_ctx.get("pokok_perjanjian", "")

        # ── Pass 0B: Dedicated signing date extraction ──────────────────────
        print("[Compliance] Pass 0B: Extracting signing date...")
        tanggal_pembuatan = extract_signing_date(full_text)

        # ── Pass 0C: Dedicated duration extraction ──────────────────────────
        print("[Compliance] Pass 0C: Extracting contract duration...")
        durasi_bulan = extract_duration_months(full_text)

        # ── Compute expiry date ─────────────────────────────────────────────
        tanggal_berakhir = None
        if tanggal_pembuatan and durasi_bulan:
            try:
                start_date = datetime.strptime(tanggal_pembuatan, "%Y-%m-%d")
                end_date = start_date + relativedelta(months=int(durasi_bulan))
                tanggal_berakhir = end_date.strftime("%Y-%m-%d")
                print(f"[Compliance] Expiry computed: {tanggal_pembuatan} + {durasi_bulan}mo = {tanggal_berakhir}")
            except Exception as e:
                print(f"[Compliance] Failed to compute expiry: {e}")
                
        # ── Pass 0D: Explicit end date fallback ─────────────────────────────
        if not tanggal_berakhir:
            print("[Compliance] Computation failed/missing, attempting explicit end date extraction...")
            tanggal_berakhir = extract_explicit_end_date(full_text)

        # ── Pass 1: Clause Extraction ───────────────────────────────────────
        print("[Compliance] Pass 1: Extracting clauses...")
        pasal_items = extract_pasal_items(text_snippet, max_items=20, max_chars_per_pasal=1200)

        # Fallback to LLM extractor for non-standard contracts
        if len(pasal_items) < 2:
            print("[Compliance] Regex found <2 clauses, falling back to LLM extractor...")
            pasal_items = llm_extract_pasal_items(full_text, max_items=20)

        # Final fallback: treat whole document as one unit
        if not pasal_items:
            pasal_items = [{"pasal": "Dokumen Keseluruhan", "deskripsi": text_snippet[:1200]}]

        print(f"[Compliance] Found {len(pasal_items)} clauses. Running analysis...")

        # ── Pass 2: Per-clause RAG + LLM Compliance Check ──────────────────
        collection = get_chroma_collection()
        results = []
        COSINE_THRESHOLD = 0.45  # tightened from 0.60 to reduce wrong-domain citations

        def worker(c):
            return process_single_pasal(c, doc_type, pihak_1, pihak_2, sektor, pokok, collection, COSINE_THRESHOLD)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for res in executor.map(worker, pasal_items[:20]):
                if res:
                    results.append(res)

        # ── Pass 4: Document-Level Summary ─────────────────────────────────
        fatal_count    = sum(1 for r in results if r["status"] == "FATAL")
        beresiko_count = sum(1 for r in results if r["status"] == "BERESIKO")
        sesuai_count   = sum(1 for r in results if r["status"] == "SESUAI")
        tidak_diatur_count = sum(1 for r in results if r["status"] == "TIDAK DIATUR")

        # Calculate score only against regulated clauses
        regulated_count = len(results) - tidak_diatur_count
        skor = round((sesuai_count / max(regulated_count, 1)) * 100) if regulated_count > 0 else 100

        summary = {
            "jenis_dokumen": doc_type,
            "pihak_pertama": pihak_1,
            "pihak_kedua": pihak_2,
            "sektor_bisnis": sektor,
            "pokok_perjanjian": pokok,
            "tanggal_berakhir": tanggal_berakhir,
            "total_klausul": len(results),
            "sesuai": sesuai_count,
            "beresiko": beresiko_count,
            "fatal": fatal_count,
            "skor_kepatuhan": skor
        }

        print(f"[Compliance] Done. Score: {summary['skor_kepatuhan']}% | "
              f"SESUAI={sesuai_count} BERESIKO={beresiko_count} FATAL={fatal_count}")

        return {"report": results, "summary": summary}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Compliance checker unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan internal saat memproses dokumen.")
    finally:
        # Automagically clean up uploaded PKS memory footprint
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except: pass

if __name__ == "__main__":
    import uvicorn
    # Natively running on port 8000
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
