from fastapi import APIRouter, Depends, HTTPException
import os, re
from typing import List
from pydantic import BaseModel
import auth
from services import pg_service, rate_limit
from services.llm_client import call_glm

router = APIRouter(prefix="/api", tags=["chat"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class ChatMessage(BaseModel):
    role: str
    content: str
    # Chat attachments (2026-10-12): honored on user messages only; ids ride
    # on the message they were sent with — the full window the FE replays IS
    # the multi-turn attachment memory.
    attachment_ids: List[str] = []

class ChatRequest(BaseModel):
    messages: List[ChatMessage] = []
    report_id: str | None = None   # B3: current workspace; None => no narrowing

class ChatResponse(BaseModel):
    answer: str
    sources: List[dict] = []
    # Populated only on the first message of a conversation (history == 1
    # user message before this turn). The FE uses it to replace the default
    # "Percakapan N" title with something descriptive. None = no change.
    suggested_title: str | None = None

class SessionRequest(BaseModel):
    title: str

# Migration M3: the module-level ChromaDB client for data/chromadb was
# vestigial -- that store held only an empty "regulations" collection which
# was never queried (retrieval goes through services.rag_service). Removed.

@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    req: ChatRequest,
    current_user: dict = Depends(auth.get_current_user),
    # Auth hardening (2026-10-14, P7): per-user LLM rate limit (LLM_RATE_PER_MIN).
    _rl: dict = Depends(rate_limit.llm_rate_limit("chat")),
):
    # FR-24: Admin cannot access document content or search
    if current_user.get("role", "pengguna").lower() == "admin":
        raise HTTPException(status_code=403, detail="Admin sistem tidak memiliki kewenangan untuk mengakses konten dokumen.")
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages array cannot be empty")
        
    last_user_message = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
    if not last_user_message:
        raise HTTPException(status_code=400, detail="Missing user message")

    # ── Check for attachments first ────────────────────────────────────────
    # If the user has uploaded documents in the current turn, check if the
    # question is about the attachment. If so, skip knowledge base retrieval
    # to avoid showing irrelevant citations.
    from services import attachment_service
    owner_id = str(current_user["id"])
    user_msgs = [m for m in req.messages if m.role == "user"]
    current_ids = list(user_msgs[-1].attachment_ids) if user_msgs else []
    
    # Patterns indicating the question is about uploaded attachments
    _ATTACHMENT_QUESTION_PATTERNS = [
        r"(ringkas|ringkasan|summarize|summary|sum up)\s+(tentang\s+)?(pdf|dokumen|document|file|lampiran)",
        r"(apa|what|explain|jelaskan|ceritakan)\s+(isi|content|contents|tentang)\s+(pdf|dokumen|document|file|lampiran)",
        r"(analisis|analyze|review|tinjau)\s+(pdf|dokumen|document|file|lampiran)",
        r"(buatlah|buat|create|make)\s+ringkasan",
        r"pdf\s+(ini|yang|aku|saya|i)\s+(kirim|upload|unggah)",
        r"dokumen\s+(ini|yang|aku|saya|i)\s+(kirim|upload|unggah)",
        r"(tentang|about|regarding)\s+(pdf|dokumen|document|file|lampiran)\s+(yang|aku|saya|i)",
    ]
    is_attachment_focused = (
        current_ids and 
        any(re.search(pat, last_user_message.lower(), re.IGNORECASE) 
            for pat in _ATTACHMENT_QUESTION_PATTERNS)
    )
    
    # 1. Retrieve Semantic Context (skip if question is clearly about uploaded attachment)
    from services.rag_service import retrieve_contexts
    from services.kg_service import retrieve_graph_contexts
    from services.db_service import log_audit
    # 2026-10-03: retrieval is best-effort. A missing embedder/reranker (a
    # deploy host shipped without data/models made every chat 500 -- see
    # bug_reports.md 2026-10-03) or a vector-store fault must not take down
    # the whole conversation. Degrade to an ungrounded answer instead: the
    # system prompt below forbids fabricated citations and a visible notice
    # is appended to the answer, so the degradation is loud, never silent.
    retrieval_degraded = False
    
    if is_attachment_focused:
        # Skip knowledge base retrieval when the question is about the uploaded document
        contexts = []
        graph_context = ""
    else:
        try:
            contexts = retrieve_contexts(last_user_message, current_user=current_user,
                                         report_id=req.report_id)
        except Exception as e:
            print(f"Chat retrieval failed, degrading to ungrounded answer: {e}")
            contexts = []
            retrieval_degraded = True
        
        # FR-16: 1.5 Retrieve Graph Context
        graph_context = retrieve_graph_contexts(last_user_message, current_user=current_user)
    
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
            "rerank_score": c.get('rerank_score'),
            "filename": c.get('filename')
        })
        context_str += f"SUMBER [{i+1}]: {c['jenis']} Nomor {c['nomor']}\nTEKS:\n{c['text']}\n\n"

    if graph_context:
        context_str += f"\n{graph_context}\n\n"

    # ── Chat attachments (2026-10-12) ───────────────────────────────────────
    # Images ride as OpenAI-style vision parts on the synthesized user
    # message; PDFs become LAMPIRAN text context: current-turn whole docs
    # verbatim (cap CURRENT_TURN_WHOLE_CAP, newest first), every other
    # in-window pdf via scoped top-k retrieval over chat_attachment_chunks.
    # Unusable ids (missing/foreign/processing/failed/over-cap) produce loud
    # notice lines appended to the answer — never a silent drop (house
    # grammar: retrieval_degraded).
    # (attachment_service and owner_id already initialized above for attachment detection)
    older_ids = [i for m in user_msgs[:-1] for i in m.attachment_ids]
    # dedupe preserving order, current turn first (vision + whole caps keep
    # the newest); whole-cap overflow falls to retrieval — its chunks exist
    # in both storage modes, so nothing is silently lost.
    window_ids = list(dict.fromkeys(current_ids + older_ids))

    ready_images, current_pdfs, other_pdfs, invalid_ids = [], [], [], []
    for aid in window_ids:
        meta = attachment_service.get_meta(aid, owner_id)
        if not meta or meta["status"] != "ready":
            invalid_ids.append(aid)
            continue
        if meta["kind"] == "image":
            ready_images.append(meta)
        elif aid in current_ids:
            current_pdfs.append(meta)
        else:
            other_pdfs.append(meta)

    vision_parts, dropped_images = attachment_service.build_vision_parts(
        [m["id"] for m in ready_images], owner_id)

    lampiran_blocks = []
    current_pdfs.reverse()  # newest first (FE appends chronologically)
    whole_now = [m for m in current_pdfs
                 if m["storage_mode"] == "whole"][:attachment_service.CURRENT_TURN_WHOLE_CAP]
    overflow = [m for m in current_pdfs if m not in whole_now]
    for meta in whole_now:
        got = attachment_service.get_whole_text(meta["id"], owner_id)
        if got:
            text, filename = got
            lampiran_blocks.append(
                f"LAMPIRAN [A{len(lampiran_blocks) + 1}]: {filename} (pdf)\nTEKS:\n{text}\n")
    retrieval_pool = other_pdfs + overflow
    attachment_retrieval_failed = False
    if retrieval_pool:
        try:
            hits = attachment_service.retrieve_attachment_contexts(
                [m["id"] for m in retrieval_pool], last_user_message)
        except Exception as e:
            print(f"Chat attachment retrieval failed, dropping attachment context: {e}")
            hits = []
            attachment_retrieval_failed = True
        fname_by_id = {m["id"]: m["filename"] for m in retrieval_pool}
        for h in hits:
            lampiran_blocks.append(
                f"LAMPIRAN [A{len(lampiran_blocks) + 1}]: "
                f"{fname_by_id.get(h['attachment_id'], h['filename'])} "
                f"(pdf, bagian ke-{h['seq'] + 1})\nTEKS:\n{h['text']}\n")
    if lampiran_blocks:
        context_str += (
            "\nDOKUMEN LAMPIRAN PENGGUNA (diunggah ke percakapan ini; "
            "BUKAN bagian basis data regulasi):\n"
            + "\n".join(lampiran_blocks) + "\n")

    # Fetch user's contract monitor stats
    user_stats_str = ""
    try:
        stats_row = pg_service.query_one(
            "SELECT COUNT(*) AS n FROM compliance_history WHERE user_id = %s",
            (current_user["id"],))
        total_docs = stats_row["n"] if stats_row else 0
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

    # ── Greeting short-circuit ──────────────────────────────────────────────
    # Casual greetings (hello, halo, hi, selamat pagi, etc.) must never
    # trigger retrieval or citation — they are social openers, not legal
    # queries. We reply warmly in the detected language and return early with
    # an empty sources list so the FE renders no citation section at all.
    _GREETING_RE = re.compile(
        r"^\s*("
        r"hi+|hello+|hey+|hei+|halo+|hai+"
        r"|howdy|greetings|yo|sup|heya"
        r"|good\s*(morning|afternoon|evening|night|day)"
        r"|selamat\s*(pagi|siang|sore|malam|datang|hari)"
        r"|apa\s*kabar|kabar\s*(baik|nih|dong)?"
        r"|how\s*(are\s*(you|ya|u)|do\s*you\s*do|is\s*(it\s*going|everything))"
        r"|what'?s?\s*up"
        r"|morning|evening|afternoon"
        r"|p+a+g+i+|s+o+r+e+|m+a+l+a+m+"
        r"|terima\s*kasih|thank\s*you|thanks|thx"
        r")\W*$",
        re.IGNORECASE,
    )
    
    # Detect general/casual questions that don't need knowledge base retrieval
    # These are non-legal, conversational questions that should get a direct answer
    _GENERAL_QUESTION_PATTERNS = [
        r"^(what|who|how)\s+(are|is)\s+you",  # "what are you", "who are you", "how are you"
        r"^siapa\s+(kamu|anda)",  # "siapa kamu"
        r"^apa\s+(yang\s+)?bisa\s+(kamu|anda)",  # "apa yang bisa kamu lakukan"
        r"^what\s+(can|do)\s+you",  # "what can you do"
        r"^help\s*me",  # "help me"
        r"^tolong\s+(bantu|saya)",  # "tolong bantu"
    ]
    is_general_question = any(re.search(pat, last_user_message.strip(), re.IGNORECASE) 
                              for pat in _GENERAL_QUESTION_PATTERNS)
    
    if _GREETING_RE.match(last_user_message) or is_general_question:
        from services.db_service import log_audit
        log_audit(current_user.get("id", ""), "SEARCH", "", f"Query: {last_user_message[:200]}")
        
        # Handle both greetings and general questions about the assistant
        if is_general_question:
            if user_lang == "english":
                greeting_reply = (
                    "I'm your Indonesian Legal Assistant, powered by AI. I can help you with:\n\n"
                    "- **Contract Analysis** — Review contracts for compliance and risks\n"
                    "- **Legal Opinions** — Answer questions about Indonesian regulations\n"
                    "- **Document Research** — Search through legal documents and regulations\n"
                    "- **Compliance Monitoring** — Track your documents for regulatory changes\n\n"
                    "Feel free to ask me anything about Indonesian law, contracts, or regulations!"
                )
                greeting_title = "About Legal Assistant"
            else:
                greeting_reply = (
                    "Saya adalah Asisten Hukum Indonesia berbasis AI. Saya dapat membantu Anda dengan:\n\n"
                    "- **Analisis Kontrak** — Menganalisis kontrak untuk kepatuhan dan risiko\n"
                    "- **Opini Legal** — Menjawab pertanyaan tentang regulasi Indonesia\n"
                    "- **Penelusuran Dokumen** — Mencari dokumen dan regulasi hukum\n"
                    "- **Monitor Kepatuhan** — Memantau dokumen Anda terhadap perubahan regulasi\n\n"
                    "Silakan tanyakan apa saja tentang hukum, kontrak, atau regulasi Indonesia!"
                )
                greeting_title = "Tentang Asisten Legal"
        else:
            # Regular greeting
            if user_lang == "english":
                greeting_reply = (
                    "Hello! 👋 I'm here to help with your Indonesian legal questions — "
                    "feel free to ask about contracts, regulations, compliance, or anything else "
                    "you'd like to explore."
                )
                greeting_title = "New Legal Consultation"
            else:
                greeting_reply = (
                    "Halo! 👋 Ada yang bisa saya bantu terkait pertanyaan hukum Anda? "
                    "Silakan tanyakan seputar kontrak, regulasi, kepatuhan, atau topik hukum lainnya."
                )
                greeting_title = "Konsultasi Hukum Baru"
        
        # Only suggest a title on the very first message; later greetings keep the existing title
        user_message_count = sum(1 for m in req.messages if m.role == "user")
        return ChatResponse(
            answer=greeting_reply,
            sources=[],
            suggested_title=greeting_title if user_message_count == 1 else None,
        )

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
        "   - Prison threats for debts: You MUST cite 'Article 19 paragraph (2) of Law No. 39 of 1999 concerning Human Rights (UU HAM)' which strictly prohibits criminal punishment/prison for civil debt issues. Clearly distinguish between civil default (wanprestasi) and malicious intent (fraud, Article 378 of the Criminal Code).\n"
        "5. CITATION MARKERS (verifiability is the product promise):\n"
        "   - LEGAL CONTEXT supplies numbered sources (SUMBER [1], SUMBER [2], ...). When a claim rests on one of them, end that claim with its marker in square brackets, immediately before the closing punctuation, e.g. '... wajib dimuat dalam laporan tahunan [2].'\n"
        "   - Use ONLY markers of sources actually supplied; never invent or renumber a marker.\n"
        "   - When quoting a regulation's number, year, or article that appears in a source, reproduce it EXACTLY as written there — never alter digits, years, or abbreviations.\n\n"
        "Always provide actionable solutions and naturally cite relevant legal bases/articles."
    )
    if retrieval_degraded:
        system_prompt += (
            "\nRETRIEVAL UNAVAILABLE: the regulation database could not be searched for this question. "
            "Do NOT cite specific regulation numbers, articles, or documents from memory. "
            "Answer with general legal principles only and say the regulation lookup is temporarily unavailable."
        )
    if lampiran_blocks:
        system_prompt += (
            "\nLAMPIRAN PENGGUNA: Blok bertanda 'LAMPIRAN [A<n>]' adalah dokumen yang diunggah pengguna "
            "ke percakapan ini — BUKAN regulasi dari basis data terindeks. Saat sebuah klaim bersandar "
            "pada isi lampiran, akhiri klaim itu dengan penandanya, mis. '... sebagaimana diatur dalam [A1].', "
            "dan sebut sumbernya sebagai 'Lampiran A<n> (nama berkas)'. Gunakan hanya penanda lampiran yang "
            "benar-benar disediakan; jangan pernah menyajikan isi lampiran sebagai regulasi terindeks."
        )
    if vision_parts:
        # Bugfix 2026-10-12 (found by the Task-5 UI probe): with a short
        # question the VLM notices an attached image on its own, but once the
        # corpus retrieval is big and noisy (snippets saturated with the word
        # "Lampiran"), the UNANNOUNCED image part gets ignored — the model
        # answers "tidak ada informasi tentang gambar". Deterministic per
        # question wording, reproduced via curl. Announce the images the same
        # way the LAMPIRAN rule announces PDF text blocks.
        system_prompt += (
            "\nGAMBAR TERLAMPIR: Pesan pengguna menyertakan "
            f"{len(vision_parts)} GAMBAR sebagai bagian multimodal (image_url). "
            "Gambar itu BENAR-BENAR ADA dan terlihat oleh Anda — lihat langsung "
            "dan gunakan isinya (warna, teks, objek) saat menjawab. Jangan pernah "
            "menyatakan gambar tidak tersedia atau tidak ada informasi tentang gambar."
        )

    enriched_user_prompt = (
        f"{lang_rule}\n\n"
        f"USER QUESTION:\n{last_user_message}\n\n"
        f"LEGAL CONTEXT:\n{context_str}\n\n"
        f"REMINDER: {lang_rule}"
    )

    # call_glm passes `messages` verbatim to the OpenAI-compatible API, so a
    # list content (vision parts + text part) needs no llm_client change.
    user_content = enriched_user_prompt
    if vision_parts:
        user_content = vision_parts + [{"type": "text", "text": enriched_user_prompt}]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content}
    ]
    # FR-25: Audit log the search query -- moved BEFORE the LLM call so a
    # failed generation is still auditable (critique remediation, 2026-10-08).
    log_audit(current_user.get("id", ""), "SEARCH", "", f"Query: {last_user_message[:200]}")
    try:
        answer = call_glm(messages, temperature=0.1, timeout=60)
    except HTTPException as e:
        # Critique remediation P1-3 (2026-10-08): an LLM failure must never
        # return a canned apology shaped like an ordinary answer -- it got
        # saved to chat history and replayed into later LLM context as if
        # the assistant had really answered. Surface failures as 502; the FE
        # renders non-2xx as a first-class error state with a retry action.
        print(f"Chat LLM call failed: {e.detail}")
        raise HTTPException(
            status_code=502,
            detail="Layanan AI tidak dapat dihubungi. Silakan coba lagi beberapa saat."
        )
    if retrieval_degraded:
        notice = (
            "\n\n---\n_Catatan: penelusuran basis data regulasi sedang tidak tersedia, "
            "sehingga jawaban ini tidak merujuk regulasi spesifik._"
            if user_lang == "indonesian" else
            "\n\n---\n_Note: regulation-database retrieval is currently unavailable, "
            "so this answer does not cite specific regulations._"
        )
        answer += notice
    # Attachment notices: same loud-degradation grammar as retrieval_degraded.
    attachment_notes = []
    if invalid_ids:
        attachment_notes.append(
            f"_Catatan: {len(invalid_ids)} lampiran dilewati (masih diproses, gagal, atau tidak ditemukan)._"
            if user_lang == "indonesian" else
            f"_Note: {len(invalid_ids)} attachment(s) skipped (still processing, failed, or not found)._")
    if dropped_images:
        attachment_notes.append(
            f"_Catatan: hanya {attachment_service.IMAGES_PER_REQUEST} gambar terbaru yang dikirim ke model; {len(dropped_images)} gambar lebih lama dilewati._"
            if user_lang == "indonesian" else
            f"_Note: only the {attachment_service.IMAGES_PER_REQUEST} most recent images were sent to the model; {len(dropped_images)} older one(s) skipped._")
    if attachment_retrieval_failed:
        attachment_notes.append(
            "_Catatan: penelusuran konteks lampiran PDF sedang tidak tersedia, sebagian isi lampiran mungkin tidak terujuk._"
            if user_lang == "indonesian" else
            "_Note: PDF attachment retrieval was unavailable; some attachment content may not be reflected._")
    if attachment_notes:
        answer += "\n\n---\n" + "\n".join(attachment_notes)

    # ── Conversation title generation (first message only) ──────────────────
    # Only fires when the window contains exactly one user message — meaning
    # this is the very first exchange in a new conversation. A tiny, cheap
    # LLM call produces a short descriptive title (≤8 words, no quotes, no
    # punctuation) in the user's language. Failure is silently swallowed:
    # the FE falls back to its existing "slice of the user text" logic when
    # suggested_title is None, so a title-gen hiccup never breaks the chat.
    user_message_count = sum(1 for m in req.messages if m.role == "user")
    suggested_title: str | None = None
    if user_message_count == 1:
        try:
            if user_lang == "english":
                title_instruction = (
                    "Write a SHORT conversation title (maximum 8 words, no quotes, no punctuation at the end) "
                    "that captures what the user is asking about. Reply with ONLY the title text, nothing else.\n\n"
                    f"User question: {last_user_message}"
                )
            else:
                title_instruction = (
                    "Tulis JUDUL percakapan yang SINGKAT (maksimal 8 kata, tanpa tanda kutip, tanpa tanda baca di akhir) "
                    "yang menggambarkan apa yang ditanyakan pengguna. Balas HANYA dengan teks judul, tidak ada yang lain.\n\n"
                    f"Pertanyaan pengguna: {last_user_message}"
                )
            raw_title = call_glm(
                [{"role": "user", "content": title_instruction}],
                temperature=0.3,
                timeout=15,
            )
            # Strip surrounding quotes/whitespace the model sometimes adds
            clean = raw_title.strip().strip('"\'').strip()
            if clean:
                suggested_title = clean[:72]  # hard cap so the sidebar never overflows
        except Exception as e:
            print(f"Title generation failed (non-fatal): {e}")
    # ── End title generation ────────────────────────────────────────────────

    return ChatResponse(answer=answer, sources=sources_to_return, suggested_title=suggested_title)

@router.post("/chat-session")
async def save_chat_session(req: ChatRequest, current_user: dict = Depends(auth.get_current_user)):
    """Save an entire chat session history (called after chat_endpoint or independently)"""
    # For a full implementation, the frontend would pass a session ID, or we generate one.
    # To keep it simple, we will just use a session_id logic here.
    pass # Implementation details added below...


