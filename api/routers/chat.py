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

    # --- Language Detection ---
    # Simple heuristic: count common English function words. If they dominate, the query is English.
    ENGLISH_WORDS = {"the","is","are","what","which","how","does","do","should","i","we","you","in","a","an","of","and","to","for","be","my","can","will","when","where","who","why","if","that","this","it","at","on","with","as","by","from","or","was","has","have","about","pay","creating","making","please","tell","me"}
    INDONESIAN_WORDS = {"apa","yang","adalah","dan","di","dalam","untuk","dengan","ini","itu","tidak","atau","bisa","harus","bagaimana","boleh","saya","anda","kamu","sebutkan","tolong","jelaskan","peraturan","undang","hukum","kontrak","perjanjian"}
    tokens = re.findall(r'\w+', last_user_message.lower())
    en_hits = sum(1 for t in tokens if t in ENGLISH_WORDS)
    id_hits = sum(1 for t in tokens if t in INDONESIAN_WORDS)
    user_lang = "english" if en_hits > id_hits else "indonesian"
    if any(phrase in last_user_message.lower() for phrase in ["in english","bahasa inggris","english please","answer in english","respond in english"]):
        user_lang = "english"
    elif any(phrase in last_user_message.lower() for phrase in ["dalam bahasa indonesia","bahasa indonesia","jawab dalam"]):
        user_lang = "indonesian"

    if user_lang == "english":
        lang_rule = "LANGUAGE: Your response MUST be entirely in English. Do NOT use Indonesian."
    else:
        lang_rule = "BAHASA: Jawablah sepenuhnya dalam Bahasa Indonesia. Jangan gunakan bahasa Inggris."

    system_prompt = (
        "You are an expert technology lawyer and contract advisor in Indonesia (Focus: ITE Law, POJK, Civil Code, PDP Law, Human Rights Law). "
        "Anda adalah pakar hukum teknologi dan penasihat kontrak di Indonesia.\n\n"
        f"{lang_rule}\n\n"
        f"{user_stats_str}"
        "STRICT GUIDELINES (WAJIB DIIKUTI):\n"
        "1. ZERO META-LANGUAGE: Never use phrases like 'Based on the provided context'. Treat legal facts as innate knowledge. Answer confidently like a real legal advisor. (Jangan gunakan frasa seperti 'Berdasarkan konteks').\n"
        "2. CONVERSATIONAL FORMAT: Do not use rigid titles like 'Legal Analysis'. Use a natural, empathetic, and professional flow. Start answers directly with 'Yes', 'No', or 'It depends' (Ya/Tidak/Tergantung). Use short paragraphs, bold text for key terms, and bullet points.\n"
        "3. STRICT MARKDOWN DISCIPLINE (output is rendered verbatim — sloppy markup is visible to the user):\n"
        "   - Bullets MUST use the exact shape `- **Label**: description` with a real label word. NEVER emit empty emphasis (`****`) or a bullet that is only a marker and a colon.\n"
        "   - NEVER break a line in the middle of a sentence. One blank line between paragraphs, no more.\n"
        "   - Bold (`**...**`) only around actual key terms, always with opening AND closing markers on the same line.\n"
        "4. CRITICAL LEGAL ARGUMENTS:\n"
        "   - Electronic vs Stamped paper contracts: Emphasize that a stamp duty (meterai) is just a document tax, NOT a requirement for a valid contract. Contract validity is based purely on Article 1320 of the Civil Code and Article 5 of the ITE Law.\n"
        "   - Prison threats for debts: You MUST cite 'Article 19 paragraph (2) of Law No. 39 of 1999 concerning Human Rights (UU HAM)' which strictly prohibits criminal punishment/prison for civil debt issues. Clearly distinguish between civil default (wanprestasi) and malicious intent (fraud, Article 378 of the Criminal Code).\n\n"
        "Always provide actionable solutions and naturally cite relevant legal bases/articles."
    )

    enriched_user_prompt = (
        f"{lang_rule}\n\n"
        f"USER QUESTION:\n{last_user_message}\n\n"
        f"LEGAL CONTEXT:\n{context_str}\n\n"
        f"REMINDER: {lang_rule}"
    )

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


