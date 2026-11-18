from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
import asyncio, os, re, json
from typing import List
from pydantic import BaseModel
import auth
from services import pg_service, rate_limit
from services.llm_client import call_llm

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
    editor_context: dict | None = None  # Live editor status from client
    rag_scope: str = "all"  # "all" | "document_only"

class ChatResponse(BaseModel):
    answer: str
    sources: List[dict] = []
    # Populated only on the first message of a conversation (history == 1
    # user message before this turn). The FE uses it to replace the default
    # "Percakapan N" title with something descriptive. None = no change.
    suggested_title: str | None = None
    action_proposal: dict | None = None
    tool_steps: List[dict] = []
    suggested_actions: List[str] = []


class ExecuteAgentActionRequest(BaseModel):
    approved_patch_ids: List[str] | None = None

def _extract_suggestions_and_clean_answer(raw_answer: str) -> tuple[str, List[str]]:
    """Strip the trailing ---SUGGESTIONS--- block and return (clean_answer, suggestions)."""
    match = re.search(r"\n*---SUGGESTIONS---\s*(.*)$", raw_answer, re.DOTALL | re.IGNORECASE)
    if not match:
        return raw_answer.strip(), []
    clean_answer = raw_answer[:match.start()].rstrip()
    suggestions: List[str] = []
    for line in match.group(1).splitlines():
        item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        item = item.strip('"\'`*').strip()
        if item and len(item) >= 4:
            suggestions.append(item[:90])
        if len(suggestions) >= 3:
            break
    return clean_answer, suggestions


def _filter_cited_sources(answer_text: str, sources: List[dict]) -> tuple[str, List[dict]]:
    """Keep only sources actually cited as [n] in answer_text and renumber sequentially."""
    if not sources:
        return answer_text, []
    cited_order: List[int] = []
    for m in re.finditer(r"\[(\d{1,2})\]", answer_text):
        idx = int(m.group(1))
        if 1 <= idx <= len(sources) and idx not in cited_order:
            cited_order.append(idx)
    if not cited_order:
        return answer_text, []
    remap = {old_idx: new_idx for new_idx, old_idx in enumerate(cited_order, start=1)}
    renumbered = re.sub(
        r"\[(\d{1,2})\]",
        lambda m: f"[{remap[int(m.group(1))]}]" if int(m.group(1)) in remap else m.group(0),
        answer_text,
    )
    return renumbered, [sources[idx - 1] for idx in cited_order]


def _fallback_suggestions(user_lang: str, has_report: bool, has_proposal: bool) -> List[str]:
    if user_lang == "english":
        if has_proposal:
            return [
                "Explain why these clause revisions reduce risk",
                "Check if any other clauses still need updates",
            ]
        if has_report:
            return [
                "Revise the remaining risky clauses",
                "Compare risky clauses against internal procedures",
                "Summarize the key obligations in this contract",
            ]
        return [
            "Check compliance requirements for this topic",
            "Draft a sample clause for this agreement",
        ]
    if has_proposal:
        return [
            "Jelaskan mengapa revisi klausul ini mengurangi risiko",
            "Periksa apakah masih ada klausul lain yang perlu diubah",
        ]
    if has_report:
        return [
            "Perbaiki klausul berisiko yang tersisa",
            "Bandingkan klausul berisiko dengan prosedur internal",
            "Ringkas kewajiban utama dalam kontrak ini",
        ]
    return [
        "Cek persyaratan regulasi untuk topik ini",
        "Buatkan contoh klausul yang sesuai",
    ]


class SessionRequest(BaseModel):
    title: str

def _build_document_context(
    report_row: dict | None,
    draft_row: dict | None,
    comments_rows: list | None,
    editor_context: dict | None = None,
) -> tuple[str, dict | None]:
    """Assembles a comprehensive, structured briefing of the active document
    (Identity, compliance score & risk breakdown, problematic clauses, and editor situation)."""
    if not report_row:
        return "", None

    filename = report_row.get("filename") or "Dokumen"
    company = report_row.get("company_name") or ""
    exp_date = report_row.get("expiration_date") or ""
    results_data = {}
    if report_row.get("results_json"):
        try:
            results_data = (
                json.loads(report_row["results_json"])
                if isinstance(report_row["results_json"], str)
                else report_row["results_json"]
            )
        except Exception:
            results_data = {}

    summary = results_data.get("summary") or {}
    clauses = results_data.get("results") or []

    # 1. Document Identity & Summary
    doc_info_parts = [
        f"- Nama Dokumen / File: {filename}",
    ]
    if company:
        doc_info_parts.append(f"- Entitas / Mitra / Label: {company}")
    if summary.get("jenis_dokumen"):
        doc_info_parts.append(f"- Jenis Dokumen: {summary['jenis_dokumen']}")
    if summary.get("pokok_perjanjian"):
        doc_info_parts.append(f"- Pokok Perjanjian / Inti Kerjasama: {summary['pokok_perjanjian']}")
    if summary.get("pihak_pertama"):
        doc_info_parts.append(f"- Pihak Pertama: {summary['pihak_pertama']}")
    if summary.get("pihak_kedua"):
        doc_info_parts.append(f"- Pihak Kedua: {summary['pihak_kedua']}")
    if summary.get("sektor_bisnis"):
        doc_info_parts.append(f"- Sektor Bisnis: {summary['sektor_bisnis']}")
    if exp_date or summary.get("tanggal_berakhir"):
        doc_info_parts.append(f"- Tanggal Berakhir: {exp_date or summary.get('tanggal_berakhir')}")

    # 2. Compliance Score & Risk Breakdown
    score = summary.get("skor_kepatuhan")
    sesuai_count = summary.get("sesuai", 0)
    beresiko_count = summary.get("beresiko", 0)
    fatal_count = summary.get("fatal", 0)
    total_klausul = summary.get("total_klausul", len(clauses))

    if not sesuai_count and not beresiko_count and not fatal_count and clauses:
        sesuai_count = sum(1 for c in clauses if (c.get("status") or "").upper() == "SESUAI")
        beresiko_count = sum(1 for c in clauses if (c.get("status") or "").upper() == "BERESIKO")
        fatal_count = sum(1 for c in clauses if (c.get("status") or "").upper() == "FATAL")
        total_klausul = len(clauses)

    score_parts = [
        f"- Skor Kepatuhan Keseluruhan: {score if score is not None else 'N/A'}%",
        f"- Total Klausul Dianalisis: {total_klausul}",
        f"- Klausul Sesuai (Aman): {sesuai_count}",
        f"- Klausul Beresiko: {beresiko_count}",
        f"- Klausul Fatal / Pelanggaran Berat: {fatal_count}",
    ]

    # 3. Specific Problematic Clauses (Beresiko & Fatal)
    problem_clauses = [
        c for c in clauses
        if (c.get("status") or "").upper() in ("BERESIKO", "FATAL")
    ]
    problem_parts = []
    if problem_clauses:
        for pc in problem_clauses[:15]:
            p_title = pc.get("pasal", "Klausul")
            p_status = (pc.get("status") or "").upper()
            p_text = (pc.get("isi_pasal") or "").strip()
            if len(p_text) > 300:
                p_text = p_text[:297] + "..."
            p_exp = (pc.get("penjelasan") or "").strip()
            p_rec = (pc.get("rekomendasi") or "").strip()
            p_reg = (pc.get("pasal_regulasi_terkait") or "").strip()

            entry = f"• [{p_status}] {p_title}:\n  - Isi Klausul: \"{p_text}\""
            if p_exp:
                entry += f"\n  - Analisis Masalah: {p_exp}"
            if p_rec:
                entry += f"\n  - Rekomendasi Perbaikan: {p_rec}"
            if p_reg:
                entry += f"\n  - Dasar Regulasi Terkait: {p_reg}"
            problem_parts.append(entry)
    else:
        problem_parts.append("Tidak ada klausul yang berstatus Beresiko atau Fatal (Semua klausul dianalisis sebagai Sesuai).")

    # 4. Current Editor Situations (Draft revisions, tracked changes, reviewer comments)
    editor_parts = []
    if draft_row and draft_row.get("version"):
        v = draft_row["version"]
        u_time = draft_row.get("updated_at") or "Baru saja"
        editor_parts.append(f"- Status Draf Editor: Tersedia Draf Revisi Aktif (Versi {v}, Terakhir Disimpan: {u_time})")
        html_body = draft_row.get("content_html") or ""
        ins_matches = re.findall(r'<ins\b[^>]*>(.*?)</ins>', html_body, re.DOTALL | re.IGNORECASE)
        del_matches = re.findall(r'<del\b[^>]*>(.*?)</del>', html_body, re.DOTALL | re.IGNORECASE)
        if ins_matches or del_matches:
            clean_ins = [re.sub(r'<[^>]+>', '', m).strip() for m in ins_matches if m.strip()][:5]
            clean_del = [re.sub(r'<[^>]+>', '', m).strip() for m in del_matches if m.strip()][:5]
            if clean_ins:
                editor_parts.append(f"- Saran Penambahan Teks (Tracked Insertions): {'; '.join(clean_ins[:3])}")
            if clean_del:
                editor_parts.append(f"- Saran Penghapusan Teks (Tracked Deletions): {'; '.join(clean_del[:3])}")
        else:
            editor_parts.append("- Perubahan Konten: Teks naskah telah disunting langsung melalui Document Editor.")
    else:
        editor_parts.append("- Status Draf Editor: Belum ada draf revisi baru tersimpan (dokumen masih dalam versi asli yang diunggah).")

    if comments_rows:
        open_cmts = [c for c in comments_rows if not c.get("resolved")]
        resolved_cmts = [c for c in comments_rows if c.get("resolved")]
        editor_parts.append(f"- Catatan/Komentar Editor: {len(open_cmts)} catatan terbuka (open), {len(resolved_cmts)} telah diselesaikan (resolved).")
        if open_cmts:
            top_notes = [f'"{c["selected_text"]}": {c["comment_text"]}' for c in open_cmts[:3] if c.get("comment_text")]
            if top_notes:
                editor_parts.append(f"- Catatan Review Utama: {'; '.join(top_notes)}")

    if editor_context:
        if editor_context.get("has_local_draft"):
            editor_parts.append("- Catatan Sesi Browser: Terdapat draf revisi aktif di sesi browser pengguna.")
        if editor_context.get("has_local_comments"):
            editor_parts.append("- Catatan Sesi Browser: Terdapat catatan review aktif di sesi browser pengguna.")

    # 5. Bilingual (ID <-> EN) Translation Discrepancies & Layout
    bilingual_section = ""
    is_bilingual = bool(
        results_data.get("is_bilingual")
        or results_data.get("layout_mode") == "bilingual_parallel"
    )
    discrepancies = results_data.get("translation_discrepancies") or []
    segments = results_data.get("segments") or []
    if is_bilingual or discrepancies:
        gov_lang = (results_data.get("governing_language") or "id").upper()
        bi_parts = [
            f"- Mode Dokumen: Kontrak Dwibahasa (ID ↔ EN), Bahasa Utama/Mengatur: {gov_lang}",
            f"- Total Segmen Terpasang: {len(segments)}",
            f"- Total Ketidaksesuaian Terjemahan Terdeteksi: {len(discrepancies)}",
        ]
        if discrepancies:
            for d in discrepancies[:15]:
                sid = d.get("segment_id") or "-"
                cat = d.get("category") or "discrepancy"
                sev = d.get("severity") or "medium"
                exp = d.get("explanation") or ""
                id_ex = (d.get("id_excerpt") or "").strip()[:160]
                en_ex = (d.get("en_excerpt") or "").strip()[:160]
                bi_parts.append(
                    f"• [{sid}] ({sev} · {cat}): {exp}"
                    + (f"\n  - ID: \"{id_ex}\"" if id_ex else "")
                    + (f"\n  - EN: \"{en_ex}\"" if en_ex else "")
                )
        else:
            bi_parts.append("- Tidak ditemukan perbedaan kewajiban (obligation shift) atau angka yang mencurigakan antara kolom ID dan EN.")
        bilingual_section = (
            "\n5. ANALISIS TERJEMAHAN DWIBAHASA (ID ↔ EN TRANSLATION DISCREPANCIES):\n"
            + "\n".join(bi_parts) + "\n"
        )

    doc_context_str = (
        "\n"
        "═══════════════════════════════════════════════════════════════════════\n"
        "INFORMASI DOKUMEN YANG SEDANG DIBUKA OLEH PENGGUNA (CURRENT OPEN DOCUMENT):\n"
        "═══════════════════════════════════════════════════════════════════════\n"
        "1. IDENTITAS & RINGKASAN DOKUMEN:\n"
        + "\n".join(doc_info_parts) + "\n\n"
        "2. SKOR KEPATUHAN & STATISTIK RISIKO (COMPLIANCE AUDIT):\n"
        + "\n".join(score_parts) + "\n\n"
        "3. KLAUSUL BERMASALAH (BERESIKO / FATAL) & REKOMENDASI:\n"
        + "\n".join(problem_parts) + "\n\n"
        "4. SITUASI / PERUBAHAN DI EDITOR DOKUMEN (DOCUMENT EDITOR SITUATION):\n"
        + "\n".join(editor_parts) + "\n"
        + bilingual_section +
        "═══════════════════════════════════════════════════════════════════════\n"
        "INSTRUKSI KHUSUS UNTUK DOKUMEN INI:\n"
        "- Anda memiliki pengetahuan LENGKAP dan LANGSUNG mengenai dokumen yang sedang dibuka di atas.\n"
        "- Saat pengguna bertanya tentang skor kepatuhan, klausul yang berisiko/fatal/sesuai, identitas kontrak, revisi di editor, atau perbedaan terjemahan ID ↔ EN, jawablah secara SPESIFIK dan AKURAT berdasarkan data resmi di atas.\n"
        "- Jika pengguna meminta saran perbaikan atau revisi klausul tertentu, gunakan klausul bermasalah dan rekomendasi di atas sebagai dasar acuan.\n"
        "- Selalu bersikap responsif dan siap membantu pengguna memperbaiki dokumen ini.\n\n"
    )

    meta_summary = {
        "filename": filename,
        "score": score,
        "beresiko_count": beresiko_count,
        "fatal_count": fatal_count,
    }
    return doc_context_str, meta_summary

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
    from services.rag_service import retrieve_contexts, list_workspace_references
    from services.kg_service import retrieve_graph_contexts
    from services.db_service import log_audit
    # 2026-10-03: retrieval is best-effort. A missing embedder/reranker (a
    # deploy host shipped without data/models made every chat 500 -- see
    # bug_reports.md 2026-10-03) or a vector-store fault must not take down
    # the whole conversation. Degrade to an ungrounded answer instead: the
    # system prompt below forbids fabricated citations and a visible notice
    # is appended to the answer, so the degradation is loud, never silent.
    retrieval_degraded = False
    
    if is_attachment_focused or (req.rag_scope == "document_only" and req.report_id):
        # Skip external knowledge base retrieval when attachment-focused or scoped to this document only
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
    
    # Format context for the prompt
    context_str = ""
    sources_to_return = []
    has_workspace_refs = False
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
        if c.get("is_workspace_ref"):
            has_workspace_refs = True
            ref_title = c.get("judul") or c.get("nomor") or "Dokumen Referensi"
            context_str += f"SUMBER [{i+1}] (DOKUMEN REFERENSI INTERNAL WORKSPACE): {c['jenis']} — {ref_title}\nTEKS:\n{c['text']}\n\n"
        else:
            context_str += f"SUMBER [{i+1}] (REGULASI PUBLIK — BUKAN DOKUMEN INTERNAL): {c['jenis']} Nomor {c['nomor']}\nTEKS:\n{c['text']}\n\n"

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

    # ── Document Context (when chatbot is opened inside an active document workspace) ──
    doc_context_str = ""
    doc_meta = None
    workspace_ref_names: list[str] = []
    target_report_id = (req.report_id or "").strip()
    if target_report_id:
        try:
            workspace_ref_names = list_workspace_references(current_user["id"], target_report_id)
        except Exception as e:
            print(f"Failed to list workspace references for report_id={target_report_id}: {e}")
        try:
            report_row = pg_service.query_one(
                "SELECT id, filename, company_name, "
                "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, "
                "results_json "
                "FROM compliance_history WHERE id = %s AND (user_id = %s OR EXISTS ("
                "  SELECT 1 FROM workspace_collaborators wc "
                "  WHERE wc.notebook_id = COALESCE(compliance_history.notebook_id, compliance_history.id) "
                "  AND wc.user_id = %s"
                "))",
                (target_report_id, current_user["id"], current_user["id"]),
            )
            draft_row = pg_service.query_one(
                "SELECT version, "
                "to_char(updated_at, 'YYYY-MM-DD HH24:MI:SS') AS updated_at, "
                "content_html "
                "FROM document_editor_drafts WHERE report_id = %s",
                (target_report_id,),
            )
            comments_rows = pg_service.query(
                "SELECT author_name, selected_text, comment_text, resolved "
                "FROM document_editor_comments WHERE report_id = %s "
                "ORDER BY created_at DESC LIMIT 10",
                (target_report_id,),
            )
            doc_context_str, doc_meta = _build_document_context(
                report_row, draft_row, comments_rows, req.editor_context
            )
        except Exception as e:
            print(f"Failed to load document context for report_id={target_report_id}: {e}")

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
            # Regular greeting (personalized with document context if opened in workspace)
            if doc_meta:
                score_val = doc_meta.get("score")
                score_txt = f"{score_val}%" if score_val is not None else "sedang diproses"
                beresiko_n = doc_meta.get("beresiko_count", 0)
                fatal_n = doc_meta.get("fatal_count", 0)
                risk_txt_parts = []
                if fatal_n:
                    risk_txt_parts.append(f"{fatal_n} fatal")
                if beresiko_n:
                    risk_txt_parts.append(f"{beresiko_n} beresiko")
                risk_summary = f" ({', '.join(risk_txt_parts)})" if risk_txt_parts else " (semua klausul sesuai)"

                if user_lang == "english":
                    greeting_reply = (
                        f"Hello! 👋 I'm your Legal Assistant for **{doc_meta['filename']}** "
                        f"(Compliance Score: {score_txt}{risk_summary}).\n\n"
                        "I have loaded full context of this contract, including its compliance audit, problematic clauses, and editor situation. "
                        "Feel free to ask about any clause, risk analysis, recommended revisions, or editor draft status!"
                    )
                    greeting_title = f"Doc: {doc_meta['filename'][:28]}"
                else:
                    greeting_reply = (
                        f"Halo! 👋 Saya adalah Asisten Legal untuk dokumen **{doc_meta['filename']}** "
                        f"(Skor Kepatuhan: {score_txt}{risk_summary}).\n\n"
                        "Saya telah memuat data lengkap dokumen ini: identitas perjanjian, hasil audit kepatuhan, rincian pasal bermasalah, dan situasi draf editor. "
                        "Silakan tanyakan tentang pasal tertentu, risiko hukum, rekomendasi perbaikan, atau perubahan di editor!"
                    )
                    greeting_title = f"Dok: {doc_meta['filename'][:28]}"
            elif user_lang == "english":
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
        f"{doc_context_str}"
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
        "   - LEGAL CONTEXT supplies numbered sources (SUMBER [1], SUMBER [2], ...). Only cite [n] when a specific legal or regulatory claim genuinely relies on that source. Do NOT cite or force sources for greetings, meta-questions, general summaries, or when the answer does not need external citations.\n"
        "   - When you do cite, end the claim with its marker in square brackets immediately before the closing punctuation, e.g. '... wajib dimuat dalam laporan tahunan [2].'\n"
        "   - Use ONLY markers of sources actually supplied; never invent or renumber a marker.\n"
        "   - When quoting a regulation's number, year, or article that appears in a source, reproduce it EXACTLY as written there — never alter digits, years, or abbreviations.\n\n"
        "6. NEXT-STEP SUGGESTIONS:\n"
        "   - At the very end of your response, after your main answer, append a section starting with the exact line `---SUGGESTIONS---` followed by 2 to 3 short, actionable next-step prompts the user can ask you to do next (written in the same language as your answer, one per line starting with `- `, max 10 words each, no bold/quotes).\n\n"
        "Always provide actionable solutions and naturally cite relevant legal bases/articles when needed."
    )
    if target_report_id:
        if workspace_ref_names:
            ref_list_str = ", ".join(workspace_ref_names)
            system_prompt += (
                f"\nDAFTAR DOKUMEN REFERENSI INTERNAL WORKSPACE: {ref_list_str}. "
                "Sumber yang ditandai '(DOKUMEN REFERENSI INTERNAL WORKSPACE)' adalah dokumen referensi khusus "
                "yang diunggah untuk ruang kerja dokumen ini. Saat meninjau, membandingkan, atau menyarankan revisi "
                "terhadap dokumen yang sedang dibuka, gunakan dan rujuk dokumen referensi internal tersebut secara aktif "
                "bersama regulasi yang berlaku, dan sertakan penanda [n]-nya. "
                "JANGAN pernah menyebut sumber bertanda '(REGULASI PUBLIK — BUKAN DOKUMEN INTERNAL)' sebagai dokumen referensi internal."
            )
        else:
            system_prompt += (
                "\nSTATUS DOKUMEN REFERENSI INTERNAL WORKSPACE: Belum ada dokumen referensi internal yang diunggah/aktif "
                "untuk workspace ini. Jika pengguna menanyakan dokumen referensi internal, sampaikan secara jujur bahwa "
                "belum ada dokumen referensi internal di workspace ini — JANGAN pernah menyebut '(REGULASI PUBLIK — BUKAN DOKUMEN INTERNAL)' "
                "sebagai dokumen referensi internal."
            )
    elif has_workspace_refs:
        system_prompt += (
            "\nDOKUMEN REFERENSI WORKSPACE: Sumber yang ditandai '(DOKUMEN REFERENSI INTERNAL WORKSPACE)' "
            "adalah dokumen referensi khusus yang diunggah untuk ruang kerja dokumen ini."
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

    # call_llm passes `messages` verbatim to the OpenAI-compatible API, so a
    # list content (vision parts + text part) needs no llm_client change.
    user_content = enriched_user_prompt
    if vision_parts:
        user_content = vision_parts + [{"type": "text", "text": enriched_user_prompt}]
    prior_turns = [
        {"role": m.role, "content": m.content}
        for m in req.messages[:-1][-6:]
        if m.role in ("user", "assistant") and m.content
    ]
    messages = (
        [{"role": "system", "content": system_prompt}]
        + prior_turns
        + [{"role": "user", "content": user_content}]
    )
    # FR-25: Audit log the search query -- moved BEFORE the LLM call so a
    # failed generation is still auditable (critique remediation, 2026-10-08).
    log_audit(current_user.get("id", ""), "SEARCH", "", f"Query: {last_user_message[:200]}")

    user_message_count = sum(1 for m in req.messages if m.role == "user")
    suggested_title: str | None = None
    action_proposal: dict | None = None
    tool_steps: list[dict] = []

    async def _run_main_answer():
        return await run_in_threadpool(call_llm, messages, temperature=0.1, timeout=300)

    async def _run_title():
        if user_message_count != 1:
            return None
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
            raw_title = await run_in_threadpool(
                call_llm,
                [{"role": "user", "content": title_instruction}],
                temperature=0.3,
                timeout=8,
                max_attempts=1,
            )
            clean = raw_title.strip().strip('"\'').strip()
            return clean[:72] if clean else None
        except Exception as e:
            print(f"Title generation failed (non-fatal): {e}")
            return None

    async def _run_proposal():
        if not target_report_id:
            return None
        try:
            from services import agent_service
            if agent_service.is_patch_intent(last_user_message):
                return await run_in_threadpool(
                    agent_service.propose_clause_patches,
                    target_report_id,
                    current_user["id"],
                    last_user_message,
                )
        except Exception as e:
            print(f"[Agent] Proposal generation skipped: {e}")
        return None

    # Run answer, optional first-turn title, and optional agentic patch proposal in parallel.
    main_res, title_res, proposal_res = await asyncio.gather(
        _run_main_answer(),
        _run_title(),
        _run_proposal(),
        return_exceptions=True,
    )
    if isinstance(main_res, HTTPException):
        print(f"Chat LLM call failed: {main_res.detail}")
        raise HTTPException(
            status_code=502,
            detail="Layanan AI tidak dapat dihubungi. Silakan coba lagi beberapa saat."
        )
    if isinstance(main_res, Exception):
        print(f"Chat LLM call failed: {main_res}")
        raise HTTPException(
            status_code=502,
            detail="Layanan AI tidak dapat dihubungi. Silakan coba lagi beberapa saat."
        )
    answer = main_res
    if isinstance(title_res, str) and title_res:
        suggested_title = title_res
    if isinstance(proposal_res, dict) and proposal_res:
        tool_steps = proposal_res.pop("tool_steps", [])
        action_proposal = proposal_res

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

    answer, suggested_actions = _extract_suggestions_and_clean_answer(answer)
    if not suggested_actions:
        suggested_actions = _fallback_suggestions(
            user_lang,
            has_report=bool(target_report_id),
            has_proposal=bool(action_proposal),
        )
    answer, sources_to_return = _filter_cited_sources(answer, sources_to_return)

    return ChatResponse(
        answer=answer,
        sources=sources_to_return,
        suggested_title=suggested_title,
        action_proposal=action_proposal,
        tool_steps=tool_steps,
        suggested_actions=suggested_actions,
    )


@router.post("/chat/actions/{action_id}/execute")
async def execute_chat_action(
    action_id: str,
    req: ExecuteAgentActionRequest | None = None,
    current_user: dict = Depends(auth.get_current_user),
):
    from services import agent_service
    approved_ids = req.approved_patch_ids if req else None
    author_name = current_user.get("username") or "Legal Advisor"
    return await run_in_threadpool(
        agent_service.execute_agent_action,
        action_id,
        current_user["id"],
        author_name,
        approved_ids,
    )


@router.post("/chat/actions/{action_id}/reject")
async def reject_chat_action(
    action_id: str,
    current_user: dict = Depends(auth.get_current_user),
):
    from services import agent_service
    return await run_in_threadpool(
        agent_service.reject_agent_action,
        action_id,
        current_user["id"],
    )

@router.post("/chat-session")
async def save_chat_session(req: ChatRequest, current_user: dict = Depends(auth.get_current_user)):
    """Save an entire chat session history (called after chat_endpoint or independently)"""
    # For a full implementation, the frontend would pass a session ID, or we generate one.
    # To keep it simple, we will just use a session_id logic here.
    pass # Implementation details added below...


