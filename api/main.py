import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import re
import json
import time
import requests
import chromadb
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
import auth
from routers import admin, engineer
from routers import chat, repository, knowledge_graph
from routers import compliance
from typing import List, Optional, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import shutil
import hashlib
import uuid
import base64
import logging
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

@app.on_event("startup")
def startup_event():
    from services.alert_scheduler import start_scheduler
    start_scheduler()

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
        c.execute("ALTER TABLE regulations ADD COLUMN klasifikasi TEXT DEFAULT 'Umum'")
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
    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        user_id TEXT,
        action_type TEXT,
        resource_id TEXT,
        details TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS kg_nodes (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        type TEXT NOT NULL,
        doc_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS kg_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        relation TEXT NOT NULL,
        doc_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS kg_exclusions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # FR-29: Document Taxonomy
    c.execute('''CREATE TABLE IF NOT EXISTS document_taxonomy (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        parent_id INTEGER,
        is_active BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Seed default taxonomy if empty
    c.execute("SELECT COUNT(*) FROM document_taxonomy")
    if c.fetchone()[0] == 0:
        default_types = [
            "Peraturan Pemerintah",
            "Undang-Undang",
            "Peraturan OJK",
            "Surat Edaran OJK",
            "Dokumen Internal",
            "Peraturan Menteri",
            "Regulasi Custom"
        ]
        for dt in default_types:
            c.execute("INSERT INTO document_taxonomy (name) VALUES (?)", (dt,))

    # Sparse Retrieval Index (BM25)
    c.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        chunk_id, doc_id UNINDEXED, text, window_context UNINDEXED
    )''')
    
    conn.commit()
    conn.close()

def log_audit(user_id: str, action_type: str, resource_id: str = "", details: str = ""):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        c = conn.cursor()
        c.execute('''INSERT INTO audit_logs (user_id, action_type, resource_id, details) 
                     VALUES (?, ?, ?, ?)''', (user_id, action_type, resource_id, details))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to write audit log: {e}")

init_main_db()

def extract_and_store_graph(doc_id: str, full_text: str, nomor: str, judul: str, jenis: str):
    """Use LLM to extract entities & relationships from a document, then upsert into KG tables."""
    import sqlite3, json as _json
    import os
    
    # FR-30: Fetch Exclusions
    exclusions = []
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        c = conn.cursor()
        c.execute("SELECT entity_name FROM kg_exclusions")
        exclusions = [row[0] for row in c.fetchall()]
        conn.close()
    except Exception as e:
        print(f"[KG] Warning: Failed to fetch exclusions: {e}")
        
    exclusion_text = ""
    if exclusions:
        exclusion_list_str = ", ".join(exclusions)
        exclusion_text = f"\n6. DILARANG KERAS mengekstrak entitas berikut ini (Abaikan mereka sepenuhnya): {exclusion_list_str}."

    snippet = full_text[:8000]  # Increased from 3500 to capture definitions (Pasal 1) and core body
    prompt = [
        {"role": "system", "content": (
            "Kamu adalah ekstraktor entitas hukum level ahli. Baca teks peraturan di bawah ini dan ekstrak "
            "entitas serta hubungan hukumnya ke dalam bentuk JSON murni. "
            "Format JSON yang diharapkan:\n"
            "{\"entitas\": [{\"id\": \"string unik\", \"label\": \"nama tampilan\", \"type\": \"entitas|topik\"}], "
            "\"relasi\": [{\"source\": \"id sumber\", \"target\": \"id target\", \"rel\": \"MENCABUT|MENGUBAH|MERUJUK|MENGATUR|DITERBITKAN_OLEH\"}]}\n"
            "Aturan Ekstraksi Kritis:\n"
            "1. Untuk type='entitas': Selalu ekstrak lembaga penerbit (e.g. OJK, Kemnaker, BI, Kemenkeu, Presiden).\n"
            "2. Untuk type='topik': Sangat penting untuk mendeteksi topik terkait skenario NDA dan PKS! Jika teks mengandung unsur 'Kerahasiaan', 'Data Pribadi', 'Keamanan Informasi', 'Rahasia Dagang', ekstrak sebagai topik NDA. Jika mengandung 'Perjanjian', 'Kontrak', 'Kemitraan', 'Vendor', 'Pengadaan', ekstrak sebagai topik PKS.\n"
            "3. Selalu buat relasi DITERBITKAN_OLEH dari regulasi ini ke lembaga penerbitnya.\n"
            "4. Gunakan nomor regulasi resmi (misal POJK-12-2023) sebagai ID untuk target relasi MERUJUK/MENGUBAH.\n"
            "5. Jangan batasi jumlah ekstraksi. Ekstrak SEMUA entitas, topik relevan, dan regulasi terkait yang ada dalam teks untuk membangun Knowledge Graph yang padat dan komprehensif."
            f"{exclusion_text}"
        )},
        {"role": "user", "content": f"Regulasi: {jenis} Nomor {nomor}\nJudul: {judul}\n\nTeks:\n{snippet}"}
    ]
    try:
        raw = call_glm(prompt, temperature=0.0, timeout=45)
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = _json.loads(raw)
        
        # FR-30 Post-processing: remove excluded entities
        if exclusions:
            exc_lower = {e.lower() for e in exclusions}
            # Filter nodes
            valid_entities = []
            excluded_ids = set()
            for ent in data.get("entitas", []):
                if ent.get("label", "").lower() in exc_lower:
                    excluded_ids.add(ent.get("id"))
                else:
                    valid_entities.append(ent)
            data["entitas"] = valid_entities
            
            # Filter edges referencing excluded nodes
            valid_relations = []
            for rel in data.get("relasi", []):
                if rel.get("source") not in excluded_ids and rel.get("target") not in excluded_ids:
                    valid_relations.append(rel)
            data["relasi"] = valid_relations

    except Exception as e:
        print(f"[KG] LLM extraction failed for {nomor}: {e}")
        return

    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        c = conn.cursor()

        # Upsert the regulation itself as a node
        reg_node_id = f"{jenis}-{nomor}".replace(" ", "-")[:80]
        c.execute("INSERT OR REPLACE INTO kg_nodes (id, label, type, doc_id) VALUES (?, ?, ?, ?)",
                  (reg_node_id, f"{jenis} {nomor}", "regulasi", doc_id))

        # Upsert extracted entities/topics
        for ent in data.get("entitas", []):
            ent_id = str(ent.get("id", "")).strip()[:80]
            ent_label = str(ent.get("label", ent_id)).strip()[:120]
            ent_type = str(ent.get("type", "topik")).strip()
            if ent_id:
                c.execute("INSERT OR IGNORE INTO kg_nodes (id, label, type, doc_id) VALUES (?, ?, ?, ?)",
                          (ent_id, ent_label, ent_type, None))

        # Insert edges
        for rel in data.get("relasi", []):
            src = str(rel.get("source", "")).strip()[:80]
            tgt = str(rel.get("target", "")).strip()[:80]
            relation = str(rel.get("rel", "MERUJUK")).strip()[:40]
            if src and tgt and src != tgt:
                # Replace regulation self-reference with the canonical reg_node_id
                if src == nomor or src == f"{jenis} {nomor}":
                    src = reg_node_id
                if tgt == nomor or tgt == f"{jenis} {nomor}":
                    tgt = reg_node_id
                c.execute("INSERT INTO kg_edges (source_id, target_id, relation, doc_id) VALUES (?, ?, ?, ?)",
                          (src, tgt, relation, doc_id))

        conn.commit()
        conn.close()
        print(f"[KG] Stored graph for {nomor}: {len(data.get('entitas',[]))} entities, {len(data.get('relasi',[]))} edges")
    except Exception as e:
        print(f"[KG] DB write failed for {nomor}: {e}")

import auth
import history
import internal_docs
import templates

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(history.router, prefix="/api", tags=["history"])
app.include_router(internal_docs.router, prefix="/api", tags=["internal_docs"])
app.include_router(templates.router, prefix="/api", tags=["templates"])

app.include_router(admin.router)
app.include_router(engineer.router)
app.include_router(chat.router)
app.include_router(repository.router)
app.include_router(knowledge_graph.router)
app.include_router(compliance.router)

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

class ConfirmPendingRequest(BaseModel):
    klasifikasi: str

@app.post("/api/repository/pending/{doc_id}/confirm")
async def confirm_pending_document(
    doc_id: str,
    req: ConfirmPendingRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))
):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    c.execute("SELECT local_path, nomor, judul, jenis, sektor FROM regulations WHERE id = ? AND status = 'Menunggu Konfirmasi'", (doc_id,))
    doc = c.fetchone()
    
    if not doc:
        conn.close()
        raise HTTPException(status_code=404, detail="Dokumen pending tidak ditemukan.")
        
    local_path, nomor, judul, jenis, sektor = doc
    
    # Update DB
    c.execute("UPDATE regulations SET status = 'Berlaku', klasifikasi = ? WHERE id = ?", (req.klasifikasi, doc_id))
    conn.commit()
    conn.close()
    
    # Trigger vector DB injection and KG asynchronously
    from routers.repository import ingest_document_background
    filename = os.path.basename(local_path)
    background_tasks.add_task(ingest_document_background, local_path, doc_id, filename, nomor, jenis, sektor, 'Berlaku', req.klasifikasi)
    
    return {"message": "Dokumen berhasil dikonfirmasi dan dimasukkan ke repositori."}

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

def get_glm_session() -> requests.Session:
    """Build a reusable HTTP session with transport-level retries."""
    global _glm_session
    if _glm_session is None:
        session = requests.Session()
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session.verify = False
        
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
    allowed_klasifikasi = ["Umum"]
    if role_level >= 2:
        allowed_klasifikasi.append("Rahasia")
    if role_level >= 3:
        allowed_klasifikasi.append("Terbatas")
        
    # 2. Fetch Explicit Access Grants & Document Classification Map
    granted_doc_ids = set()
    doc_klasifikasi_map = {}
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT doc_id FROM access_grants WHERE granted_to = ? AND expires_at >= datetime('now')", (user_id,))
        for row in c.fetchall():
            granted_doc_ids.add(row[0])
            
        c.execute("SELECT id, klasifikasi FROM regulations")
        for row in c.fetchall():
            doc_klasifikasi_map[row[0]] = row[1] or "Umum"
        conn.close()
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
    # Fallback to Hybrid Retrieval (Dense + BM25 Sparse) & RRF
    chunk_data = {}
    dense_ranks = {}
    sparse_ranks = {}
    
    # --- 1. Dense Retrieval (ChromaDB) ---
    dense_res = collection.query(
        query_texts=[query],
        n_results=overfetch_n,
        where=where_clause
    )
    
    dense_rank = 1
    if dense_res and dense_res['documents']:
        for i in range(len(dense_res['documents'][0])):
            meta = dense_res['metadatas'][0][i]
            reg_id = meta.get("reg_id")
            
            if not is_allowed(reg_id):
                continue
                
            chunk_id = dense_res['ids'][0][i]
            base_doc = dense_res['documents'][0][i]
            window_doc = meta.get('window_context', base_doc)
            
            dense_ranks[chunk_id] = dense_rank
            chunk_data[chunk_id] = {
                "id": chunk_id,
                "text": window_doc,
                "jenis": meta.get('jenis', ''),
                "nomor": meta.get('nomor', ''),
                "sektor": meta.get('sektor', ''),
                "judul": meta.get('judul', '')
            }
            dense_rank += 1

    # --- 2. Sparse Retrieval (SQLite FTS5 BM25) ---
    import string
    # Clean query for FTS MATCH syntax to avoid OperationalErrors
    safe_query = query.replace('"', '').replace("'", "")
    safe_query = " OR ".join([word for word in safe_query.split() if len(word) > 2])
    
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        c = conn.cursor()
        
        # We fetch extra because we still need to filter by is_allowed
        c.execute("""
            SELECT chunk_id, doc_id, text, window_context
            FROM chunks_fts 
            WHERE chunks_fts MATCH ? 
            ORDER BY rank 
            LIMIT ?
        """, (safe_query, overfetch_n * 2))
        
        sparse_rank = 1
        for row in c.fetchall():
            chunk_id, doc_id, text, window_context = row
            if not is_allowed(doc_id):
                continue
                
            sparse_ranks[chunk_id] = sparse_rank
            
            # If Chroma didn't find this chunk, we need to populate its data
            if chunk_id not in chunk_data:
                # To get metadata like 'judul', we query the main table
                c.execute("SELECT judul, nomor, jenis, sektor FROM regulations WHERE id = ?", (doc_id,))
                reg_row = c.fetchone()
                if reg_row:
                    judul, nomor, jenis, sektor = reg_row
                    chunk_data[chunk_id] = {
                        "id": chunk_id,
                        "text": window_context if window_context else text,
                        "jenis": jenis or '',
                        "nomor": nomor or '',
                        "sektor": sektor or '',
                        "judul": judul or ''
                    }
            sparse_rank += 1
            
        conn.close()
    except Exception as e:
        print(f"BM25 Sparse Retrieval Error: {e}")

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

def retrieve_graph_contexts(query: str, current_user: dict, max_nodes=10) -> str:
    """FR-16: Queries the SQLite Knowledge Graph based on query keywords and returns a formatted graph string."""
    import sqlite3
    import os
    import re
    
    # 1. Clean query to extract keywords
    stopwords = {"apa", "siapa", "kapan", "dimana", "mengapa", "bagaimana", "dan", "atau", "di", "ke", "dari", "yang", "untuk", "dengan", "tentang", "terkait", "saja", "apakah"}
    words = re.findall(r'\b\w+\b', query.lower())
    keywords = [w for w in words if w not in stopwords and len(w) > 3]
    
    if not keywords:
        return ""
        
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 2. Find matching nodes
        query_conditions = " OR ".join(["label LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]
        
        c.execute(f"SELECT id, label, type FROM kg_nodes WHERE {query_conditions} LIMIT {max_nodes}", params)
        matched_nodes = c.fetchall()
        
        if not matched_nodes:
            conn.close()
            return ""
            
        matched_node_ids = [row[0] for row in matched_nodes]
        
        # 3. Find 1-degree connections
        placeholders = ",".join(["?"] * len(matched_node_ids))
        c.execute(f"""
            SELECT e.source_id, n1.label, e.relation, e.target_id, n2.label 
            FROM kg_edges e
            LEFT JOIN kg_nodes n1 ON e.source_id = n1.id
            LEFT JOIN kg_nodes n2 ON e.target_id = n2.id
            WHERE e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders})
            LIMIT 40
        """, matched_node_ids + matched_node_ids)
        
        edges = c.fetchall()
        conn.close()
        
        if not edges:
            return ""
            
        # 4. Format into natural text
        graph_text = "STRUKTUR KNOWLEDGE GRAPH (Entitas dan Relasi yang relevan dengan pertanyaan):\n"
        for src_id, src_label, relation, tgt_id, tgt_label in edges:
            s_lbl = src_label or src_id
            t_lbl = tgt_label or tgt_id
            graph_text += f"- [{s_lbl}] {relation} [{t_lbl}]\n"
            
        return graph_text
    except Exception as e:
        print(f"Graph retrieval error: {e}")
        return ""

@app.delete("/api/repository/failed")
async def delete_failed_documents(current_user: dict = Depends(auth.require_role("manajer"))):
    """Deletes all documents that failed processing (e.g., Duplicates)."""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    c.execute("SELECT id, local_path FROM regulations WHERE status LIKE 'Gagal%'")
    failed_docs = c.fetchall()
    
    deleted_count = 0
    for doc in failed_docs:
        doc_id, local_path = doc
        # Delete physical file
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception as e:
                print(f"Error deleting file {local_path}: {e}")
        # Delete from DB
        c.execute("DELETE FROM regulations WHERE id = ?", (doc_id,))
        deleted_count += 1
        
    conn.commit()
    conn.close()
    
    return {"message": f"Berhasil menghapus {deleted_count} dokumen yang gagal."}

