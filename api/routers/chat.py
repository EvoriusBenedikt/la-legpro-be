from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
import sqlite3, os, json, time, re
from typing import List, Optional, Any
from pydantic import BaseModel
import auth
import chromadb
from services.llm_client import call_glm

router = APIRouter(prefix="/api", tags=["chat"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage] = []

class ChatResponse(BaseModel):
    answer: str
    sources: List[dict] = []

class SessionRequest(BaseModel):
    title: str

try:
    chroma_client = chromadb.PersistentClient(path=os.path.join(BASE_DIR, "data", "chromadb"))
    collection = chroma_client.get_or_create_collection(name="regulations")
except Exception as e:
    print("ChromaDB Error in chat router:", e)
    collection = None

@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest, current_user: dict = Depends(auth.get_current_user)):
    # FR-24: Admin cannot access document content or search
    if current_user.get("role", "pengguna").lower() == "admin":
        raise HTTPException(status_code=403, detail="Admin sistem tidak memiliki kewenangan untuk mengakses konten dokumen.")
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages array cannot be empty")
        
    last_user_message = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
    if not last_user_message:
        raise HTTPException(status_code=400, detail="Missing user message")

    # 1. Retrieve Semantic Context
    from main import retrieve_contexts, retrieve_graph_contexts, log_audit
    contexts = retrieve_contexts(last_user_message, current_user=current_user)
    
    # FR-16: 1.5 Retrieve Graph Context
    graph_context = retrieve_graph_contexts(last_user_message, current_user=current_user)
    
    # Format context for the prompt
    context_str = ""
    sources_to_return = []
    for i, c in enumerate(contexts):
        sources_to_return.append({
            "id": c['id'],
            "jenis": c['jenis'],
            "nomor": c['nomor'],
            "sektor": c['sektor'],
            "judul": c['judul'],
            "snippet": c['text'],
            "rerank_score": c.get('rerank_score')
        })
        context_str += f"SUMBER [{i+1}]: {c['jenis']} Nomor {c['nomor']}\nTEKS:\n{c['text']}\n\n"

    if graph_context:
        context_str += f"\n{graph_context}\n\n"

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
        print(f"Chat fallback triggered due to GLM error: {e.detail}")
        answer = (
            "Maaf, layanan AI sedang mengalami gangguan koneksi sementara. "
            "Silakan coba kirim pertanyaan yang sama beberapa detik lagi."
        )
    # FR-25: Audit log the search query
    log_audit(current_user.get("id", ""), "SEARCH", "", f"Query: {last_user_message[:200]}")
    return ChatResponse(answer=answer, sources=sources_to_return)

@router.post("/chat-session")
async def save_chat_session(req: ChatRequest, current_user: dict = Depends(auth.get_current_user)):
    """Save an entire chat session history (called after chat_endpoint or independently)"""
    # For a full implementation, the frontend would pass a session ID, or we generate one.
    # To keep it simple, we will just use a session_id logic here.
    pass # Implementation details added below...


