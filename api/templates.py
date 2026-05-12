from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auth import get_current_user, get_db_connection

router = APIRouter()

from dotenv import load_dotenv
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

API_BASE_URL = os.getenv("MODEL_BASE_URL", "https://console.labahasa.ai/v1").rstrip("/")
API_KEY      = os.getenv("MODEL_API_KEY", "")
LLM_MODEL    = os.getenv("LLAMA_MODEL", "llama-4-maverick-instruct")


def _make_llm_session() -> requests.Session:
    """Return a requests Session with automatic retry on connection errors."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    return session


class TemplateGenerateRequest(BaseModel):
    template_id: str
    user_prompt: str


@router.get("/templates")
def get_templates(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, title, description, content_template, category, created_at "
        "FROM document_templates ORDER BY created_at DESC"
    )
    templates = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"templates": templates}


@router.post("/templates/generate")
def generate_document_from_template(
    req: TemplateGenerateRequest,
    current_user: dict = Depends(get_current_user)
):
    # 1. Fetch the template from SQLite
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM document_templates WHERE id = ?", (req.template_id,))
    template_row = c.fetchone()
    conn.close()

    if not template_row:
        raise HTTPException(status_code=404, detail="Template not found")

    template_content = template_row["content_template"]

    # 2. Build the prompt
    system_prompt = (
        "Anda adalah AI asisten hukum Indonesia yang ahli dalam menyusun dokumen hukum. "
        "Tugas Anda adalah menghasilkan draft dokumen hukum yang lengkap dan profesional "
        "berdasarkan template standar dan instruksi spesifik pengguna.\n\n"
        "ATURAN PENTING:\n"
        "- Isi SEMUA placeholder dalam kurung siku [] dengan informasi dari instruksi pengguna.\n"
        "- Jika ada informasi yang tidak disebutkan, gunakan placeholder yang lebih deskriptif.\n"
        "- Output hanya berisi teks dokumen lengkap dalam format Markdown.\n"
        "- Jangan menambahkan kalimat pengantar atau penutup.\n\n"
        f"--- TEMPLATE DASAR ---\n{template_content}\n--- AKHIR TEMPLATE ---"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"Instruksi kustomisasi: {req.user_prompt}"}
    ]

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.5,
        "stream": False
    }

    # 3. Call the LLM with automatic retry on transient drops
    session = _make_llm_session()
    try:
        resp = session.post(
            f"{API_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=90
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="LLM request timed out. Please try again.")
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Koneksi ke LLM terputus. Coba lagi dalam beberapa detik. ({str(e)[:100]})")

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail=f"LLM returned error {resp.status_code}: {resp.text[:300]}"
        )

    try:
        generated_text = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected LLM response format: {str(e)}"
        )

    return {"generated_document": generated_text}
