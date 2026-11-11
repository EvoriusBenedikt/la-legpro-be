"""Canonical LLM transport client (OpenAI-compatible GLM/VLM endpoints).

Phase 2 refactor note: this module previously held an older, simpler copy of
call_glm / get_glm_session / is_transient_network_error. It has been replaced
with the newer implementation moved verbatim from api/main.py. The external
contract (URL handling, payload, retry env knobs, HTTPException status codes
and user-facing messages) is unchanged; the newer version additionally logs
llm_metrics, prints debug info, and uses a hardened transport session.
Vision helpers (call_glm_vision, vlm_extract_page_image, VLM_PAGE_PROMPT)
also moved here from api/main.py.
"""
import os
import json
import re
import base64
import requests
from typing import Optional
from fastapi import HTTPException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services import pg_service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── GLM API Config ──────────────────────────────────────────────────────────
API_BASE_URL = os.getenv("MODEL_BASE_URL", "https://console.labahasa.ai/v1").rstrip("/")
API_KEY      = os.getenv("MODEL_API_KEY", "")
# Reverted to use Maverick for both text and vision because GLM is unstable
GLM_MODEL    = os.getenv("LLM_MODEL", "llama-4-maverick-instruct")
VLM_MODEL    = os.getenv("LLM_MODEL", "llama-4-maverick-instruct")
GLM_MAX_ATTEMPTS = int(os.getenv("GLM_MAX_ATTEMPTS", "5"))
GLM_RETRY_BACKOFF_BASE = float(os.getenv("GLM_RETRY_BACKOFF_BASE", "0.8"))

if not API_BASE_URL or not API_KEY:
    print("[WARNING] MODEL_BASE_URL or MODEL_API_KEY is not set in .env — LLM calls will fail.")

_glm_session: Optional[requests.Session] = None

GENERIC_GLM_ERROR_MESSAGE = (
    "Layanan AI sedang mengalami gangguan koneksi sementara. "
    "Silakan coba lagi dalam beberapa saat."
)

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


def extract_json(raw: str):
    """Parse the JSON object/array out of an LLM response (Migration M4).

    llama-4-maverick via MODEL_BASE_URL sometimes prefixes a prose preamble
    ("Berikut adalah ...:\\n```json\\n{...}\\n```") or appends trailing
    commentary, after which a bare json.loads on the fence-stripped text
    fails -- this silently killed KG extraction during the M3 smokes (see
    bug_reports.md). Strategy: strip markdown fences anywhere, try a direct
    parse, then fall back to the outermost {...} span, then the outermost
    [...] span (some prompts ask for bare arrays).

    Raises json.JSONDecodeError when nothing parseable is found, so the
    pre-existing `except json.JSONDecodeError` handlers at call sites keep
    working unchanged.
    """
    text = re.sub(r'```json|```', '', raw).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Candidate spans: outermost {...} and outermost [...], tried in order of
    # where they OPEN -- a prose-wrapped array [{"a":1}] must win over the
    # object nested inside it (fixed after the M4 unit battery caught the
    # fixed-order version returning the inner object).
    spans = []
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = text.find(open_ch), text.rfind(close_ch)
        if i != -1 and j > i:
            spans.append((i, text[i:j + 1]))
    for _, candidate in sorted(spans, key=lambda s: s[0]):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise json.JSONDecodeError(
        "no parseable JSON object/array in LLM response", text, 0)


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
    print(f"DEBUG: API Key configured: {'Yes' if API_KEY else 'No (EMPTY)'}")

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
            import time
            start_time = time.time()
            # Tuple timeout: (connect_timeout, read_timeout)
            resp = session.post(url, json=payload, headers=headers, timeout=(15, timeout))
            latency_ms = int((time.time() - start_time) * 1000)

            if not resp.ok:
                # Include truncated upstream body to speed up debugging bad credentials/model/payload.
                body_snippet = (resp.text or "")[:300]
                print(f"GLM non-2xx response: status={resp.status_code}, body={body_snippet}")
                raise HTTPException(
                    status_code=500,
                    detail=GENERIC_GLM_ERROR_MESSAGE
                )

            data = resp.json()
            tokens = data.get("usage", {}).get("total_tokens", 0)
            cost = (tokens / 1000.0) * 0.001
            try:
                pg_service.execute(
                    "INSERT INTO llm_metrics (endpoint, tokens_used, latency_ms, cost_estimate) "
                    "VALUES (%s, %s, %s, %s)",
                    ("chat_completions", tokens, latency_ms, cost),
                )
            except Exception:
                pass
            return data["choices"][0]["message"]["content"]
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


def vlm_extract_page_image(page) -> str:
    """
    Render a PyMuPDF page as a crisp JPEG image and return base64-encoded string.
    Uses 2x scale (~192 DPI) with 88% JPEG quality: ~9x smaller payload than PNG
    with zero loss in OCR legibility, drastically reducing network transmission and latency.
    """
    import fitz
    mat = fitz.Matrix(2, 2)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("jpeg", jpg_quality=88)).decode("utf-8")


def call_glm_vision(image_b64: str, prompt: str, timeout: int = 90, mime_type: str = "image/jpeg") -> str:
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
                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}
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
    import time
    start_time = time.time()
    resp = get_glm_session().post(url, json=payload, headers=headers, timeout=(15, timeout))
    latency_ms = int((time.time() - start_time) * 1000)

    if resp.ok:
        data = resp.json()
        tokens = data.get("usage", {}).get("total_tokens", 0)
        cost = (tokens / 1000.0) * 0.001
        try:
            pg_service.execute(
                "INSERT INTO llm_metrics (endpoint, tokens_used, latency_ms, cost_estimate) "
                "VALUES (%s, %s, %s, %s)",
                ("vlm_vision", tokens, latency_ms, cost),
            )
        except Exception:
            pass
        return data["choices"][0]["message"]["content"]
    print(f"VLM page extraction failed: {resp.status_code} {resp.text[:200]}")
    return ""


VLM_PAGE_PROMPT = (
    "Ekstrak SEMUA teks dari dokumen ini secara akurat dengan panduan berikut:\n"
    "1. Teks Biasa & Judul: Ekstrak semua teks secara lengkap dan akurat. Perbaiki salah ketik/typo visual hasil OCR "
    "(misalnya huruf 'l' yang seharusnya 'I' atau 'J') agar membentuk kalimat bahasa Indonesia baku yang masuk akal. "
    "Pertahankan penomoran pasal, ayat, dan hierarki dokumen.\n"
    "2. Tabel & Matriks: Jika halaman memiliki tabel, format sebagai tabel Markdown standar (| Kolom 1 | Kolom 2 |) "
    "dengan baris header dan pemisah yang rapi. "
    "Setiap tabel WAJIB memakai satu baris header, baris pemisah `|---|---|`, dan jumlah kolom yang SAMA di setiap baris. "
    "Jangan menyisipkan baris kosong di tengah tabel; sel kosong tulis sebagai spasi. Tabel yang berlanjut dari halaman sebelumnya tetap ditulis sebagai tabel.\n"
    "3. Diagram Alir & Flowchart (misal SOP / Prosedur Eskalasi / Swimlane / Metodologi): Jangan memotong teks secara acak melintasi kolom. "
    "Tulis judul prosedur sebagai `### Diagram Alir: <judul>`, lalu setiap swimlane/pihak sebagai `**Swimlane: <pihak>**`, "
    "lalu langkah-langkahnya sebagai daftar bernomor berurutan (`1. ...`), tuliskan keputusan sebagai `Jika <kondisi> -> <langkah berikutnya>`. "
    "Jangan menggambarkan diagram sebagai tabel satu kolom.\n"
    "4. Tanda Tangan & Paraf: Jangan mentranskripsi coretan tinta atau paraf tangan menjadi karakter acak (seperti ';;;...' atau 'nOZI'). "
    "Untuk SETIAP blok tanda tangan yang terlihat di halaman (termasuk yang hanya berupa coretan/stempel), tulis satu baris tersendiri "
    "dengan format `[Tanda Tangan: <Nama Perusahaan> - <Nama Penandatangan>, <Jabatan>]` (atau `[Paraf: <Nama Pihak>]`), "
    "ditempatkan persis pada posisi urutan dokumen di mana tanda tangan itu muncul. Jangan melewatkan blok tanda tangan.\n"
    "5. Format keluaran: Keluarkan HANYA isi dokumen. DILARANG menulis kalimat pembuka/penutup seperti 'Berikut adalah ekstraksi teks...', "
    "DILARANG memakai pagar kode (```), dan DILARANG memakai penebalan markdown `**...**` untuk teks biasa "
    "(gunakan `**` hanya untuk label swimlane). Jangan menambahkan komentar meta atau opini di luar teks dokumen. "
    "Jangan mengulang judul pasal dua kali."
)
