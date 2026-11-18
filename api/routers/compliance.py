from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
import os, json, re, uuid, shutil, base64
from datetime import datetime
from dateutil.relativedelta import relativedelta
import concurrent.futures
from typing import Optional, Any
from pydantic import BaseModel, field_validator
import auth
from fastapi.concurrency import run_in_threadpool
from services import pg_service, rate_limit
from services import favorability
from services.llm_client import call_llm, extract_json

# M3 migration note: SQLite (legal_metadata.db) and ChromaDB are replaced by
# PostgreSQL + pgvector. Regulation metadata lives in `regulations`; chunks +
# embeddings live in the unified `chunks` table (schema owned by migrations/,
# document text ordered by seq).

router = APIRouter(prefix="/api", tags=["compliance"])
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")

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

@router.post("/upload")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...), 
    doc_type: str = Form("regulations"),
    klasifikasi: str = Form("Umum"),
    current_user: dict = Depends(auth.require_role("manajer"))
):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
    # check file size without reading into memory completely if possible
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")
        
    clean_filename = os.path.basename(file.filename)
    if not clean_filename or clean_filename in ('.', '..'):
        raise HTTPException(status_code=400, detail="Nama file tidak valid.")

    # Setup directories
    pdfs_dir = os.path.join(BASE_DIR, "data", "pdfs")
    os.makedirs(pdfs_dir, exist_ok=True)
    
    unique_prefix = str(uuid.uuid4())[:8]
    stored_filename = f"{unique_prefix}_{clean_filename}"
    file_path = os.path.join(pdfs_dir, stored_filename)
    
    # Save physical file
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        # ── Proceed with Saving Metadata (schema owned by migrations/) ───────
                      
        # Generate ID and Metadata
        doc_id = unique_prefix
        judul = clean_filename.replace('.pdf', '')
        nomor = f"CUSTOM-{doc_id}"
        
        if doc_type == "internal":
            jenis = "Dokumen Internal"
            sektor = "Dokumen Internal"
        else:
            jenis = "Regulasi Custom"
            sektor = "Upload Manual"
        
        status = "Memproses" # Initialize with processing status
        
        # detail_url stays NULL (not ""): regulations.detail_url is UNIQUE and
        # an empty string would collide across custom uploads (legacy bug:
        # the second custom upload always failed). NULL never violates UNIQUE.
        rows = pg_service.execute_returning(
            '''INSERT INTO regulations 
               (judul, download_url, nomor, jenis, sektor, status, detail_url, local_path, klasifikasi, domain) 
               VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s)
               RETURNING id''',
            (judul, "", nomor, jenis, sektor, status, file_path, klasifikasi, "Custom"))
        # Get the actual generated ID
        doc_id = str(rows[0]["id"])
        
        # Upload-history intake (2026-09-30, migration 006): durable trail row
        # for the Riwayat Unggahan tab; pipeline transitions in repository.py
        # update it (awaiting_approval / auto_approved / failed_* / ...).
        from services.db_service import log_upload_intake
        log_upload_intake(doc_id, clean_filename, judul, current_user)
        
        # Launch async background task for actual text extraction and chunking
        from routers.repository import process_document_background
        from services import task_queue
        task_queue.enqueue(doc_id, "parse", f"Parsing (AI Review): {clean_filename}", clean_filename,
                           (file_path, doc_id, clean_filename, nomor, jenis, sektor, status, klasifikasi),
                           uploader=current_user.get("username"))
        background_tasks.add_task(process_document_background, file_path, doc_id, clean_filename, nomor, jenis, sektor, status, klasifikasi)
        
        return {"status": "success", "message": "Dokumen berhasil diunggah dan sedang diproses di latar belakang."}
        
    except Exception as e:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        print(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat memproses dokumen.")

def _sibling_chunk_doc_id(reg_id):
    """doc_id of a duplicate sibling holding chunks, or None.

    Primary match key = chunks.filename (the local_path basename serve_pdf
    also resolves on) + domain: duplicate registrations of one regulation
    share the same source file (reg 2 resolves to its chunk-holding sibling
    reg 1 -- both POJK_Perbankan_9.pdf). Nomor/judul columns alone are not
    reliable across duplicate registrations, and some legacy chunks carry
    NULL filename, so a nomor + judul-prefix pass runs as second fallback.
    """
    row = pg_service.query_one(
        "SELECT domain, local_path, nomor, judul FROM regulations "
        "WHERE id::text = %s",
        (reg_id,))
    if not row:
        return None
    basename = os.path.basename(
        str(row["local_path"] or "").replace("\\", "/"))
    if basename:
        sib = pg_service.query_one(
            "SELECT c.doc_id FROM chunks c "
            "JOIN regulations s ON s.id::text = c.doc_id "
            "WHERE c.filename = %s AND s.domain = %s AND c.doc_id <> %s "
            "GROUP BY c.doc_id ORDER BY c.doc_id LIMIT 1",
            (basename, row["domain"], reg_id))
        if sib:
            return sib["doc_id"]
    # Second pass: nomor + judul-prefix match, for siblings whose chunks
    # carry NULL/mismatched filename metadata (163 of doc 462's chunks have
    # no filename at all).
    sib = pg_service.query_one(
        "SELECT s.id::text AS doc_id FROM regulations r "
        "JOIN regulations s ON s.domain = r.domain AND s.nomor = r.nomor "
        "  AND LEFT(s.judul, 60) = LEFT(r.judul, 60) AND s.id <> r.id "
        "WHERE r.id::text = %s "
        "  AND EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = s.id::text) "
        "ORDER BY s.id LIMIT 1",
        (reg_id,))
    return sib["doc_id"] if sib else None


def _fetch_doc_chunks(reg_id, nomor):
    """Chunks for a regulation by reg_id, with duplicate-sibling fallback.

    The corpus carries duplicate registrations of the same regulation (e.g.
    PG ids 1 and 2 for POJK 9/2024 -- reg 1 holds the chunks) where only one
    row carries the legacy chunk ingest; the chunk-less duplicate used to
    render an empty drawer
    ("belum memiliki data di knowledge base"). Per user decision 2026-10-07
    (non-destructive sibling fallback; corpus dedup deferred), a reg_id
    lookup that finds nothing resolves via _sibling_chunk_doc_id. The nomor
    path already matches across rows via chunks.nomor and needs no fallback.
    """
    if not reg_id:
        return pg_service.query(
            "SELECT text FROM chunks WHERE nomor = %s ORDER BY seq", (nomor,))
    rows = pg_service.query(
        "SELECT text FROM chunks WHERE doc_id = %s ORDER BY seq", (reg_id,))
    if rows:
        return rows
    sib = _sibling_chunk_doc_id(reg_id)
    if not sib:
        return []
    return pg_service.query(
        "SELECT text FROM chunks WHERE doc_id = %s ORDER BY seq", (sib,))


# Auth hardening (2026-10-14, D2/P7): these LLM endpoints used to be open to the
# internet. /analyze and /analyze-pasals need a logged-in user (any role, incl.
# insinyur ti: the engineer monitoring drawer calls them) plus a per-user rate limit.
@router.post("/analyze")
async def analyze_document(
    req: AnalyzeRequest,
    current_user: dict = Depends(auth.get_current_user),
    _rl: dict = Depends(rate_limit.llm_rate_limit("analyze")),
):
    """Analyzes a document from regulation chunks + LLM for overview and status."""
    
    # Step 1: Fetch all chunks for this specific document
    # Prefer reg_id (unique) but fall back to nomor if not available
    try:
        rows = _fetch_doc_chunks(req.reg_id, req.nomor)
        chunks = [r["text"] for r in rows]
    except Exception as e:
        print(f"Chunks fetch error: {e}")
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
        # call_llm is blocking `requests` code; awaited in a worker thread so
        # the single-threaded event loop keeps serving everyone else while the
        # LLM call runs (previously froze the whole backend for the call's
        # duration — healthz included; bug_reports.md 2026-10-07).
        raw_content = await run_in_threadpool(call_llm, messages_llm, temperature=0.1, timeout=60)
        parsed = extract_json(raw_content)
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

@router.post("/analyze-pasals")
async def analyze_pasals(
    req: AnalyzeRequest,
    current_user: dict = Depends(auth.get_current_user),
    _rl: dict = Depends(rate_limit.llm_rate_limit("analyze_pasals")),
):
    """
    Deep analysis: uses LLM to analyze each Pasal individually,
    producing status, perbandingan (comparison), and hubungan (hierarchy).
    """
    try:
        rows = _fetch_doc_chunks(req.reg_id, req.nomor)
        chunks = [r["text"] for r in rows]
    except Exception as e:
        print(f"Chunks fetch error: {e}")
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
        # Same event-loop protection as /analyze: the deep per-pasal call is
        # the longest LLM round trip in the drawer (timeout=180, plus the
        # retry chain) and must not freeze other users' requests.
        raw = await run_in_threadpool(
            call_llm,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            timeout=180,
        )

        # The LLM might return an object {"pasals":[...]} OR a bare array [...]
        # (M4: extract_json also survives prose around the JSON body)
        parsed = extract_json(raw)
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


def _rerank_candidate_regulations(
    desc: str,
    candidates: list[dict],
    min_score: Optional[float] = None,
    max_keep: int = 5,
) -> list[dict]:
    """Filter/rank borderline regulation candidates (0.28 <= dist < COSINE_THRESHOLD)
    using the local CrossEncoder reranker instead of a per-clause LLM call.

    Falls back to distance-sorted top-3 candidates if the reranker is unavailable.
    """
    if not candidates:
        return []
    if min_score is None:
        min_score = float(os.environ.get("RERANKER_MIN_SCORE", "-2.0"))
    try:
        from services.rag_service import get_reranker
        reranker = get_reranker()
        pairs = [[desc[:500], c["text"][:500]] for c in candidates]
        scores = reranker.predict(pairs)
        scored = sorted(
            zip(candidates, scores),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        kept = [c for c, score in scored if float(score) >= min_score][:max_keep]
        return kept
    except Exception as e:
        print(f"  [Reranker] Falling back to cosine order: {e}")
        return sorted(candidates, key=lambda c: float(c["row"]["dist"]))[:3]


def _retrieve_clause_context(
    c: dict,
    doc_type: str,
    sektor: str,
    COSINE_THRESHOLD: float,
    uid: str,
    excluded: set,
    report_id: Optional[str],
    contract_categories=None,
    precomputed_vec=None,
) -> Optional[dict]:
    """Retrieve and cross-encoder-rerank supporting regulations for one clause."""
    pasal_label = c.get("pasal", "Pasal")
    desc = c.get("deskripsi", "")
    if not desc or len(desc) < 10:
        return None

    doc_cat = "NDA" if "NDA" in str(doc_type).upper() or "NON-DISCLOSURE" in str(doc_type).upper() else "PKS"

    if precomputed_vec is not None:
        query_vec = precomputed_vec
    else:
        search_query = f"[{doc_type}] [{sektor}] {desc[:400]}"
        from services.embed_service import embed_query
        query_vec = embed_query(search_query)
    cat_filter = list(contract_categories) if contract_categories else None
    with pg_service.get_conn() as conn:
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        if cat_filter:
            pub_cur = conn.execute(
                "SELECT c.id, c.doc_id, c.text, c.jenis, c.nomor, c.sektor, "
                "c.embedding <=> %s::vector AS dist, "
                "r.id AS regulation_id, r.legal_status, r.status_note, r.last_verified_at, r.detail_url "
                "FROM chunks c "
                "LEFT JOIN regulations r ON r.id::text = c.doc_id "
                "WHERE c.embedding IS NOT NULL "
                "AND COALESCE(c.is_active_law, TRUE) = TRUE "
                "AND (c.doc_id IS NULL OR COALESCE(r.legal_status, 'berlaku') IN ('berlaku', 'diubah', 'dicabut_sebagian')) "
                "AND c.doc_category = ANY(%s) AND c.category_code = ANY(%s) "
                "ORDER BY c.embedding <=> %s::vector LIMIT 8",
                (query_vec, [doc_cat, "UMUM", "regulations"], cat_filter, query_vec))
        else:
            pub_cur = conn.execute(
                "SELECT c.id, c.doc_id, c.text, c.jenis, c.nomor, c.sektor, "
                "c.embedding <=> %s::vector AS dist, "
                "r.id AS regulation_id, r.legal_status, r.status_note, r.last_verified_at, r.detail_url "
                "FROM chunks c "
                "LEFT JOIN regulations r ON r.id::text = c.doc_id "
                "WHERE c.embedding IS NOT NULL "
                "AND COALESCE(c.is_active_law, TRUE) = TRUE "
                "AND (c.doc_id IS NULL OR COALESCE(r.legal_status, 'berlaku') IN ('berlaku', 'diubah', 'dicabut_sebagian')) "
                "AND c.doc_category = ANY(%s) "
                "ORDER BY c.embedding <=> %s::vector LIMIT 8",
                (query_vec, [doc_cat, "UMUM", "regulations"], query_vec))
        pub_rows = pub_cur.fetchall()

        int_cur = conn.execute(
            "SELECT c.id, c.doc_id, c.text, c.jenis, c.nomor, c.sektor, "
            "c.embedding <=> %s::vector AS dist, "
            "NULL::integer AS regulation_id, 'berlaku' AS legal_status, "
            "NULL::text AS status_note, NULL::timestamptz AS last_verified_at, NULL::text AS detail_url "
            "FROM chunks c "
            "WHERE c.embedding IS NOT NULL "
            "AND c.visibility = 'private' AND c.user_id = %s::text "
            "AND c.doc_id IS NULL AND c.id LIKE 'internal\\_%%' "
            "AND (c.report_id IS NULL OR c.report_id = %s) "
            "ORDER BY c.embedding <=> %s::vector LIMIT 5",
            (query_vec, uid, report_id, query_vec))
        int_rows = int_cur.fetchall()

        seen_ids = set()
        sr_rows = []
        for r in int_rows + pub_rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                sr_rows.append(r)

    from services.rag_service import chunk_source_ref
    sr_rows = [r for r in sr_rows
               if chunk_source_ref(r["doc_id"], r["id"]) not in excluded]

    supporting_regulations = []
    relevant_refs = []

    if sr_rows:
        within_thresh = []
        for row in sr_rows:
            if float(row["dist"]) < COSINE_THRESHOLD:
                within_thresh.append({
                    "row": row,
                    "text": row["text"],
                    "reg_name": f"{row['jenis'] or 'Aturan'} Nomor {row['nomor'] or 'N/A'}",
                })
        if within_thresh:
            # High-confidence matches (< 0.28) are kept directly; borderline
            # matches (0.28..0.45) are scored by the CrossEncoder reranker.
            need_check = [cand for cand in within_thresh if float(cand["row"]["dist"]) >= 0.28]
            auto_keep = [cand for cand in within_thresh if float(cand["row"]["dist"]) < 0.28]
            kept_checked = _rerank_candidate_regulations(desc, need_check) if need_check else []

            matched_reg_ids = [
                cand["row"]["regulation_id"] for cand in (auto_keep + kept_checked)
                if cand["row"].get("regulation_id") is not None
            ]
            amendment_map = {}
            if matched_reg_ids:
                from services.freshness.live_verifier import get_amendment_chain
                with pg_service.get_conn() as conn:
                    amendment_map = get_amendment_chain(conn, matched_reg_ids)

            for cand in auto_keep + kept_checked:
                row = cand["row"]
                reg_id = row.get("regulation_id")
                legal_status = row.get("legal_status") or "berlaku"
                amended_by = amendment_map.get(reg_id, []) if reg_id is not None else []
                lv_at = row.get("last_verified_at")
                supporting_regulations.append({
                    "regulation_id": reg_id,
                    "jenis": row['jenis'] or 'Aturan',
                    "nomor": row['nomor'] or 'N/A',
                    "sektor": row['sektor'] or 'Umum',
                    "teks": cand["text"],
                    "legal_status": legal_status,
                    "status_note": row.get("status_note"),
                    "last_verified_at": lv_at.isoformat() if hasattr(lv_at, "isoformat") else lv_at,
                    "detail_url": row.get("detail_url"),
                    "amended_by": amended_by,
                })
                amend_note = ""
                if amended_by:
                    labels = ", ".join(a.get("canonical_key") or "" for a in amended_by if a.get("canonical_key"))
                    amend_note = f"\n[Catatan Perubahan: {legal_status.upper()} oleh {labels}]"
                relevant_refs.append(f"--- {cand['reg_name']} ({legal_status}) ---{amend_note}\n{cand['text'][:700]}")

    return {
        "pasal_label": pasal_label,
        "desc": desc,
        "supporting_regulations": supporting_regulations,
        "relevant_refs": relevant_refs,
    }


def _unregulated_clause_result(ctx: dict) -> dict:
    """Return the standard result when no supporting regulation matched a clause."""
    return {
        "pasal": ctx["pasal_label"],
        "isi_pasal": ctx["desc"],
        "status": "TIDAK DIATUR",
        "penjelasan": "Tidak ditemukan regulasi yang secara langsung relevan dengan klausul ini dalam database saat ini.",
        "rekomendasi": "Pastikan klausul ini wajar secara keperdataan dan tidak bertentangan dengan KUH Perdata.",
        "ai_analysis": "",
        "supporting_regulations": ctx["supporting_regulations"],
        **favorability.empty_position(),
    }


def _build_clause_result(ctx: dict, ans: dict) -> dict:
    """Normalize a parsed LLM compliance verdict dict into the report row shape."""
    status_raw = str(ans.get("status", "BERESIKO")).upper()
    if "SESUAI" in status_raw:
        status_final = "SESUAI"
    elif "FATAL" in status_raw:
        status_final = "FATAL"
    elif "TIDAK DIATUR" in status_raw:
        status_final = "TIDAK DIATUR"
    else:
        status_final = "BERESIKO"

    return favorability.apply_position({
        "pasal": ctx["pasal_label"],
        "isi_pasal": ctx["desc"],
        "status": status_final,
        "pasal_regulasi_terkait": ans.get("pasal_regulasi_terkait", ""),
        "penjelasan": ans.get("penjelasan", "Gagal diverifikasi"),
        "rekomendasi": ans.get("rekomendasi", ""),
        "ai_analysis": ans.get("ai_analysis", ""),
        "supporting_regulations": ctx["supporting_regulations"],
    }, ans)


def _verify_single_clause(
    ctx: dict,
    doc_type: str,
    pihak_1: str,
    pihak_2: str,
    sektor: str,
    pokok: str,
    pihak_kita_nama: str = "",
    peran_kita: Optional[str] = None,
) -> dict:
    """Run single-clause LLM compliance verification given retrieved context."""
    pasal_label = ctx["pasal_label"]
    desc = ctx["desc"]
    supporting_regulations = ctx["supporting_regulations"]
    relevant_refs = ctx["relevant_refs"]

    if not relevant_refs:
        return _unregulated_clause_result(ctx)

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
        "2. Kesesuaian: Apakah klausul ini SECARA EKSPLISIT diatur, dilarang, atau diwajibkan oleh satu atau lebih regulasi / dokumen internal di atas?\n"
        "   Jika melanggar atau terkait dengan LEBIH DARI SATU regulasi atau dokumen internal, sebutkan SEMUANYA secara eksplisit (pisahkan dengan '; ').\n"
        "3. Risiko: Identifikasi seluruh risiko hukum & ketidaksesuaian prosedur internal secara konkret.\n"
        "4. Verdict: Berikan satu dari tiga status:\n"
        "   - SESUAI: klausul patuh dan tidak berisiko\n"
        "   - BERESIKO: klausul perlu perbaikan atau klarifikasi\n"
        "   - FATAL: klausul melanggar ketentuan wajib regulasi\n\n"

        + favorability.position_prompt_block(pihak_kita_nama or pihak_1, peran_kita) +

        "Kembalikan HANYA JSON berikut (tanpa markdown, tanpa teks lain):\n"
        '{\n'
        '  "status": "SESUAI" | "BERESIKO" | "FATAL",\n'
        '  "pasal_regulasi_terkait": "misal: Pasal 15 ayat (3) PP 45/2015",\n'
        '  "penjelasan": "penjelasan spesifik 1-2 kalimat merujuk regulasi",\n'
        '  "rekomendasi": "saran perbaikan konkret (kosongkan jika SESUAI)",\n'
        '  "ai_analysis": "dampak bisnis jangka panjang (isi hanya jika BERESIKO atau FATAL)",\n'
        '  "kelebihan": [{"poin": "...", "alasan": "...", "dasar": "hukum"}],\n'
        '  "kekurangan": [{"poin": "...", "alasan": "...", "dasar": "komersial"}],\n'
        '  "posisi_perusahaan": "MENGUNTUNGKAN" | "NETRAL" | "MERUGIKAN",\n'
        '  "catatan_posisi": "satu kalimat"\n'
        '}'
    )

    try:
        raw_v = call_llm(
            [{"role": "user", "content": prompt_verify}],
            temperature=0.1,
            timeout=90
        )
        ans = extract_json(raw_v)
        return _build_clause_result(ctx, ans)

    except HTTPException as e:
        print(f"[Compliance] Verification HTTP error: {e.detail}")
        return {
            "pasal": pasal_label, "isi_pasal": desc, "status": "ERROR",
            "pasal_regulasi_terkait": "",
            "penjelasan": "Layanan AI sedang mengalami gangguan koneksi sementara. Silakan coba lagi dalam beberapa saat.",
            "rekomendasi": "Coba ulang analisis.",
            "ai_analysis": "", "supporting_regulations": supporting_regulations,
            **favorability.empty_position(),
        }
    except Exception as e:
        print(f"[Compliance] Parsing error for {pasal_label}: {e}")
        return {
            "pasal": pasal_label, "isi_pasal": desc, "status": "ERROR",
            "pasal_regulasi_terkait": "",
            "penjelasan": "Format respons LLM tidak valid.",
            "rekomendasi": "-",
            "ai_analysis": "", "supporting_regulations": supporting_regulations,
            **favorability.empty_position(),
        }


def verify_clauses_batch(
    batch_contexts: list[dict],
    doc_type: str,
    pihak_1: str,
    pihak_2: str,
    sektor: str,
    pokok: str,
    pihak_kita_nama: str = "",
    peran_kita: Optional[str] = None,
) -> list[dict]:
    """Verify a small batch of clauses (e.g. 3-5) in one LLM call, falling back
    to single-clause verification for any missing or unparseable item."""
    if not batch_contexts:
        return []
    if len(batch_contexts) == 1:
        return [
            _verify_single_clause(
                batch_contexts[0], doc_type, pihak_1, pihak_2, sektor, pokok,
                pihak_kita_nama, peran_kita,
            )
        ]

    clause_blocks = []
    for idx, ctx in enumerate(batch_contexts):
        combined_regs = "\n\n".join(ctx["relevant_refs"])
        clause_blocks.append(
            f"=== ITEM {idx} ({ctx['pasal_label']}) ===\n"
            f"KLAUSUL:\n{ctx['desc']}\n\n"
            f"REGULASI TERKAIT ITEM {idx}:\n{combined_regs}"
        )

    items_text = "\n\n".join(clause_blocks)
    prompt_batch = (
        "Anda adalah auditor kepatuhan hukum senior Indonesia yang sangat teliti. "
        "Analisis setiap item klausul kontrak di bawah ini secara terpisah dan mendalam menggunakan regulasi yang menyertainya.\n\n"

        f"KONTEKS DOKUMEN:\n"
        f"- Jenis: {doc_type}\n"
        f"- Pihak Pertama: {pihak_1}\n"
        f"- Pihak Kedua: {pihak_2}\n"
        f"- Sektor: {sektor}\n"
        f"- Pokok Perjanjian: {pokok}\n\n"

        f"{items_text}\n\n"

        "INSTRUKSI ANALISIS UNTUK SETIAP ITEM (ikuti urutan ini):\n"
        "1. Identifikasi: Apa topik hukum utama klausul item tersebut? (pembayaran, kerahasiaan, PHK, dll)\n"
        "2. Kesesuaian: Apakah klausul item tersebut SECARA EKSPLISIT diatur, dilarang, atau diwajibkan oleh satu atau lebih regulasi / dokumen internal pada item tersebut?\n"
        "   Jika melanggar atau terkait dengan LEBIH DARI SATU regulasi atau dokumen internal, sebutkan SEMUANYA secara eksplisit (pisahkan dengan '; ').\n"
        "3. Risiko: Identifikasi seluruh risiko hukum & ketidaksesuaian prosedur internal secara konkret.\n"
        "4. Verdict: Berikan satu dari tiga status:\n"
        "   - SESUAI: klausul patuh dan tidak berisiko\n"
        "   - BERESIKO: klausul perlu perbaikan atau klarifikasi\n"
        "   - FATAL: klausul melanggar ketentuan wajib regulasi\n\n"

        + favorability.position_prompt_block(pihak_kita_nama or pihak_1, peran_kita) +

        f"Kembalikan HANYA JSON objek dengan key \"results\" berisi array tepat {len(batch_contexts)} objek (satu untuk setiap id 0..{len(batch_contexts) - 1}, tanpa markdown, tanpa teks lain):\n"
        '{\n'
        '  "results": [\n'
        '    {\n'
        '      "id": 0,\n'
        '      "status": "SESUAI" | "BERESIKO" | "FATAL",\n'
        '      "pasal_regulasi_terkait": "misal: Pasal 15 ayat (3) PP 45/2015",\n'
        '      "penjelasan": "penjelasan spesifik 1-2 kalimat merujuk regulasi",\n'
        '      "rekomendasi": "saran perbaikan konkret (kosongkan jika SESUAI)",\n'
        '      "ai_analysis": "dampak bisnis jangka panjang (isi hanya jika BERESIKO atau FATAL)",\n'
        '      "kelebihan": [{"poin": "...", "alasan": "...", "dasar": "hukum"}],\n'
        '      "kekurangan": [{"poin": "...", "alasan": "...", "dasar": "komersial"}],\n'
        '      "posisi_perusahaan": "MENGUNTUNGKAN" | "NETRAL" | "MERUGIKAN",\n'
        '      "catatan_posisi": "satu kalimat"\n'
        '    }\n'
        '  ]\n'
        '}'
    )

    by_id: dict[int, dict] = {}
    try:
        raw_v = call_llm(
            [{"role": "user", "content": prompt_batch}],
            temperature=0.1,
            timeout=120,
        )
        parsed = extract_json(raw_v)
        items = parsed.get("results") if isinstance(parsed, dict) else parsed
        if isinstance(items, list):
            for pos, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                try:
                    item_id = int(item.get("id", pos))
                except (TypeError, ValueError):
                    item_id = pos
                if 0 <= item_id < len(batch_contexts) and item_id not in by_id:
                    by_id[item_id] = item
    except Exception as e:
        print(f"[Compliance] Batch verify fallback triggered ({len(batch_contexts)} clauses): {e}")

    out: list[dict] = []
    for idx, ctx in enumerate(batch_contexts):
        if idx in by_id:
            out.append(_build_clause_result(ctx, by_id[idx]))
        else:
            out.append(
                _verify_single_clause(
                    ctx, doc_type, pihak_1, pihak_2, sektor, pokok,
                    pihak_kita_nama, peran_kita,
                )
            )
    return out


def process_single_pasal(c, doc_type, pihak_1, pihak_2, sektor, pokok, COSINE_THRESHOLD, uid, excluded, report_id,
                         pihak_kita_nama="", peran_kita=None, contract_categories=None, precomputed_vec=None):
    ctx = _retrieve_clause_context(
        c, doc_type, sektor, COSINE_THRESHOLD, uid, excluded, report_id,
        contract_categories=contract_categories,
        precomputed_vec=precomputed_vec,
    )
    if ctx is None:
        return None
    return _verify_single_clause(
        ctx, doc_type, pihak_1, pihak_2, sektor, pokok,
        pihak_kita_nama, peran_kita,
    )

def extract_docx_content(file_path: str, filename: str, use_ocr: bool = False) -> str:
    """
    Extracts text from DOCX documents with robust multi-layer handling:
    1. Standard paragraphs, table cells, Content Controls (w:sdt), floating textboxes (w:txbxContent),
       headers/footers, and DrawingML shapes in natural document order.
    2. Scanned pages / embedded images in word/media/:
       If extracted text < 100 chars (scanned document / image-heavy docx),
       extracts page images in document sequence, normalizes them via Pillow (supporting PNG, JPEG,
       single/multi-page TIFF, BMP, GIF, WEBP), and passes them through VLM in parallel.
    3. Fallback to PyMuPDF rendering if images are vector metafiles (EMF/WMF) or non-standard DOCX.
    """
    import io
    import re
    import docx
    import zipfile
    import xml.etree.ElementTree as ET
    from PIL import Image
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from services.llm_client import call_llm_vision, VLM_PAGE_PROMPT, vlm_extract_page_image
    from services.rag_service import extract_text_hybrid

    try:
        fsize = os.path.getsize(file_path)
    except Exception:
        fsize = -1
    print(f"  [DOCX] Inspecting {filename} ({fsize} bytes, use_ocr={use_ocr})")

    # If a PDF was renamed to .docx, route to hybrid PDF extractor
    try:
        with open(file_path, "rb") as fh:
            magic = fh.read(5)
        if magic == b"%PDF-":
            print(f"  [DOCX] File {filename} has %PDF- header; routing to extract_text_hybrid")
            return extract_text_hybrid(file_path, force_vlm=use_ocr)
    except Exception:
        pass

    full_text = []
    seen_snippets = set()

    def _add_line(txt: str):
        cleaned = txt.strip()
        if cleaned:
            full_text.append(cleaned)
            seen_snippets.add(cleaned)

    # 1. Standard paragraphs and tables in natural document body order
    try:
        doc = docx.Document(file_path)
        for child in doc.element.body:
            tag = child.tag.split('}')[-1]
            if tag == 'p':
                p = Paragraph(child, doc)
                _add_line(p.text)
            elif tag == 'tbl':
                tbl = Table(child, doc)
                for row in tbl.rows:
                    row_data = [cell.text.strip().replace('\n', ' ') for cell in row.cells if cell.text.strip()]
                    if row_data:
                        _add_line(" | ".join(row_data))

        # Walk any remaining w:p in body (captures w:sdt, w:txbxContent, v:textbox)
        try:
            for p_elem in doc.element.xpath('.//w:p'):
                txt = "".join(p_elem.xpath('.//w:t/text()')).strip()
                if txt and txt not in seen_snippets:
                    _add_line(txt)
        except Exception as xe:
            print(f"  [DOCX] w:p xpath scan warning: {xe}")

        # DrawingML shapes text (a:p / a:t)
        try:
            for p_elem in doc.element.xpath('.//a:p'):
                txt = "".join(p_elem.xpath('.//a:t/text()')).strip()
                if txt and txt not in seen_snippets:
                    _add_line(txt)
        except Exception:
            pass

        # Headers & footers
        try:
            for section in doc.sections:
                for hf in (section.header, section.footer):
                    if hf:
                        for p in hf.paragraphs:
                            if p.text.strip() and p.text.strip() not in seen_snippets:
                                _add_line(p.text)
        except Exception:
            pass
    except Exception as e:
        print(f"  [DOCX] Standard text parser error for {filename}: {e}")

    extracted_text = "\n".join(full_text).strip()
    print(f"  [DOCX] Native XML text extracted: {len(extracted_text)} chars")

    # Native digital DOCX with substantial XML text is already lossless — return directly
    if len(extracted_text) >= 100:
        return extracted_text

    # 2. Embedded image extraction (scanned DOCX fallback or forced OCR)
    try:
        if zipfile.is_zipfile(file_path):
            with zipfile.ZipFile(file_path, 'r') as z:
                rels_map = {}
                if 'word/_rels/document.xml.rels' in z.namelist():
                    rels_root = ET.fromstring(z.read('word/_rels/document.xml.rels'))
                    for rel in rels_root:
                        rid = rel.get('Id')
                        target = rel.get('Target', '')
                        if 'media/' in target:
                            if not target.startswith('word/'):
                                target = 'word/' + target.lstrip('/')
                            rels_map[rid] = target

                ordered_images = []
                if 'word/document.xml' in z.namelist():
                    doc_root = ET.fromstring(z.read('word/document.xml'))
                    for elem in doc_root.iter():
                        for attr_val in elem.attrib.values():
                            if attr_val in rels_map:
                                img_path = rels_map[attr_val]
                                if img_path in z.namelist() and img_path not in ordered_images:
                                    ordered_images.append(img_path)

                all_media = [
                    f for f in z.namelist()
                    if f.startswith('word/media/')
                ]
                def natural_sort_key(s):
                    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]
                all_media.sort(key=natural_sort_key)

                for img in all_media:
                    if img not in ordered_images:
                        ordered_images.append(img)

                media_summary = [(m, z.getinfo(m).file_size) for m in ordered_images]
                print(f"  [DOCX] Found {len(ordered_images)} media item(s) in archive: {media_summary[:15]}")

                # Filter out tiny decorative icons (< 10KB)
                candidate_images = [img for img in ordered_images if z.getinfo(img).file_size > 10000]

                # Convert candidate images (including multi-frame TIFFs) into normalized PNG base64 pages
                page_b64_list: list[str] = []
                for img_name in candidate_images:
                    if len(page_b64_list) >= 30:
                        break
                    try:
                        raw_bytes = z.read(img_name)
                        with Image.open(io.BytesIO(raw_bytes)) as pil_img:
                            n_frames = getattr(pil_img, "n_frames", 1)
                            for frame_idx in range(min(n_frames, 30 - len(page_b64_list))):
                                if n_frames > 1:
                                    pil_img.seek(frame_idx)
                                rgb_img = pil_img.convert("RGB")
                                # Skip tiny dimensions (e.g. banners/logos < 150px high)
                                if rgb_img.width < 150 or rgb_img.height < 150:
                                    continue
                                rgb_img.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
                                out_buf = io.BytesIO()
                                rgb_img.save(out_buf, format="PNG", optimize=True)
                                page_b64_list.append(base64.b64encode(out_buf.getvalue()).decode("utf-8"))
                    except Exception as img_err:
                        print(f"  [DOCX] Pillow could not decode {img_name}: {img_err}")

                if page_b64_list:
                    print(f"  [DOCX->VLM] Submitting {len(page_b64_list)} normalized page image(s) to VLM...")
                    def _ocr_page(args: tuple[int, str]) -> tuple[int, str]:
                        idx, b64_data = args
                        try:
                            txt = call_llm_vision(b64_data, VLM_PAGE_PROMPT, timeout=75)
                            if txt and txt.strip():
                                print(f"    [DOCX->VLM] Page {idx}/{len(page_b64_list)} extracted ({len(txt.strip())} chars)")
                                return idx, f"--- Halaman {idx} ---\n{txt.strip()}"
                        except Exception as ve:
                            print(f"    [DOCX->VLM] Page {idx} error: {ve}")
                        return idx, ""

                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                        results = list(pool.map(_ocr_page, enumerate(page_b64_list, start=1)))

                    results.sort(key=lambda x: x[0])
                    vlm_pages_text = [text for _, text in results if text]
                    if vlm_pages_text:
                        combined_vlm = "\n\n".join(vlm_pages_text)
                        if extracted_text:
                            return f"{extracted_text}\n\n{combined_vlm}"
                        return combined_vlm
    except Exception as ze:
        print(f"  [DOCX] Embedded image extraction error for {filename}: {ze}")

    # 3. Final fallback: PyMuPDF direct document page rendering (handles EMF/WMF or complex layouts)
    try:
        import fitz
        fitz_doc = fitz.open(file_path)
        if len(fitz_doc) > 0:
            print(f"  [DOCX->PyMuPDF] Rendering {len(fitz_doc)} page(s) via PyMuPDF fallback...")
            vlm_pages = []
            for pno in range(min(len(fitz_doc), 25)):
                page = fitz_doc.load_page(pno)
                p_txt = page.get_text("text").strip()
                if len(p_txt) >= 80 and not use_ocr:
                    vlm_pages.append(p_txt)
                else:
                    b64_img = vlm_extract_page_image(page)
                    ocr_txt = call_llm_vision(b64_img, VLM_PAGE_PROMPT, timeout=75)
                    if ocr_txt and ocr_txt.strip():
                        vlm_pages.append(f"--- Halaman {pno + 1} ---\n{ocr_txt.strip()}")
            fitz_doc.close()
            if vlm_pages:
                return "\n\n".join(vlm_pages)
    except Exception as fe:
        print(f"  [DOCX->PyMuPDF] Fallback error: {fe}")

    return extracted_text


def extract_text_multi_format(file_path: str, filename: str, use_ocr: bool = False) -> str:
    from services.rag_service import extract_text_hybrid
    from services.llm_client import call_llm_vision, VLM_PAGE_PROMPT
    """
    Extracts text from various file formats: PDF, DOCX, XLSX, PPTX, JPG, PNG, TXT.
    Routes to the appropriate extractor based on the file extension.
    """
    ext = filename.lower().split('.')[-1]
    
    if ext == 'pdf':
        # (R1 bugfix: use_ocr=true used to force the traditional-OCR parser,
        # which was dead in every shipped image -- scanned pages came back as
        # "[HALAMAN BERUPA GAMBAR - OCR GAGAL]" placeholders. Both toggle
        # states now route through the hybrid extractor; true = force_vlm,
        # the documented "compliance mode". See la-legpro-doc/bug_reports.md.)
        if use_ocr:
            print(f"  [OCR->VLM] Forcing VLM extraction for {filename}")
            return extract_text_hybrid(file_path, force_vlm=True)
        else:
            return extract_text_hybrid(file_path, force_vlm=False)
        
    elif ext == 'txt':
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
            
    elif ext in ['jpg', 'jpeg', 'png']:
        # (R1 bugfix: the traditional-OCR image path was dead in every shipped
        # image -- parser.ocr was always None, so use_ocr=true returned
        # "[OCR GAGAL INISIALISASI]". Both toggle states now use the VLM path.)
        print(f"  [VLM] Image extraction for {filename}")
        with open(file_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')
        mime = f"image/{'png' if ext == 'png' else 'jpeg'}"
        vlm_text = call_llm_vision(img_b64, VLM_PAGE_PROMPT, timeout=60, mime_type=mime)
        return vlm_text.strip()
        
    elif ext == 'docx':
        return extract_docx_content(file_path, filename, use_ocr=use_ocr)
            
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


# require_role("pengguna") blocks insinyur ti (business feature) and is declared
# before the limiter so a 403 never consumes rate-limit budget.
@router.post("/check-compliance")
async def check_compliance(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    use_ocr: str = Form("false"),
    report_id: str | None = Form(None),   # B3: current workspace; None => no narrowing
    pihak_kita: str = Form("pertama"),    # "pertama" | "kedua" — whose side we score
    peran_kita: str | None = Form(None),  # Penyedia | Pelanggan | Pemberi Kerja | Penerima Kerja
    current_user: dict = Depends(auth.require_role("pengguna")),
    _rl: dict = Depends(rate_limit.llm_rate_limit("check_compliance")),
):
    """
    Accepts a document upload (PDF, DOCX, XLSX, PPTX, JPG, PNG, TXT), extracts text,
    and uses a multi-step LLM pipeline to cross-check clauses against the OJK repository.

    All blocking work (file parsing, multiple LLM round trips, ThreadPoolExecutor clause
    analysis) is offloaded to a threadpool via run_in_threadpool so the uvicorn event loop
    remains free to serve other requests while the analysis runs.  The same fix was applied
    to /analyze and /analyze-pasals (bug_reports.md 2026-10-07); this endpoint had the same
    problem and is now patched consistently.
    """
    allowed_exts = ['.pdf', '.docx', '.xlsx', '.pptx', '.jpg', '.jpeg', '.png', '.txt']
    ext = os.path.splitext(file.filename.lower())[1]

    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Format file tidak didukung. Harap unggah PDF, DOCX, XLSX, PPTX, JPG, PNG, atau TXT.")

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    upload_id = str(uuid.uuid4())[:8]
    clean_filename = os.path.basename(file.filename)
    if not clean_filename or clean_filename in ('.', '..'):
        raise HTTPException(status_code=400, detail="Nama file tidak valid.")
    temp_path = os.path.join(PDFS_DIR, f"temp_check_{upload_id}_{clean_filename}")

    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # ── Blocking pipeline extracted to a sync function ───────────────────────
    # run_in_threadpool hands this to a worker thread so the event loop is
    # free throughout the multi-minute analysis (file parsing, 4-6 sequential
    # LLM calls, up to 20 parallel per-clause LLM calls via ThreadPoolExecutor).
    # HTTPException is re-raised transparently; any other exception is wrapped
    # in a 500 by the outer handler.
    def _run_pipeline() -> dict:
        from services.rag_service import (
            understand_document, extract_signing_date, extract_duration_months,
            extract_explicit_end_date, extract_pasal_items, llm_extract_pasal_items,
            get_user_exclusions,
        )

        # 🔍 Pass 0: Multi-Format Extraction ──────────────────────────────────
        print(f"[Compliance] Starting extraction for: {file.filename}")
        is_ocr = use_ocr.lower() == "true"
        bilingual_meta = None
        if not is_ocr:
            try:
                from services import bilingual_extractor
                fname_lower = (file.filename or "").lower()
                if fname_lower.endswith(".pdf"):
                    bilingual_meta = bilingual_extractor.extract_bilingual_from_pdf(temp_path)
                elif fname_lower.endswith(".docx"):
                    bilingual_meta = bilingual_extractor.extract_bilingual_from_docx(temp_path)
            except Exception as be:
                print(f"[Compliance] Bilingual pre-scan skipped: {be}")
                bilingual_meta = None

        if bilingual_meta and bilingual_meta.get("is_bilingual") and bilingual_meta.get("segments"):
            from services import bilingual_extractor
            gov_lang = bilingual_meta.get("governing_language") or "id"
            full_text = bilingual_extractor.build_governing_text(bilingual_meta["segments"], gov_lang)
            if len(full_text.strip()) < 100:
                full_text = extract_text_multi_format(temp_path, file.filename, use_ocr=is_ocr)
        else:
            full_text = extract_text_multi_format(temp_path, file.filename, use_ocr=is_ocr)
        text_snippet = full_text[:250000]

        if len(text_snippet.strip()) < 100:
            raise HTTPException(
                status_code=400,
                detail="Dokumen tidak dapat dibaca atau teks kurang dari 100 karakter. Jika dokumen berupa pindaian (scan) atau gambar dalam DOCX/PDF, pastikan gambar terbaca jelas atau aktifkan opsi OCR."
            )

        # ── Pass 0A + 0B + 0C in Parallel ────────────────────────────────
        print("[Compliance] Pass 0A/0B/0C: Extracting metadata, signing date & duration in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p0_pool:
            fut_ctx = p0_pool.submit(understand_document, full_text)
            fut_date = p0_pool.submit(extract_signing_date, full_text)
            fut_dur = p0_pool.submit(extract_duration_months, full_text)
            doc_ctx = fut_ctx.result()
            tanggal_pembuatan = fut_date.result()
            durasi_bulan = fut_dur.result()

        doc_type = doc_ctx.get("jenis_dokumen", "Kontrak")
        pihak_1  = doc_ctx.get("pihak_pertama", "Pihak Pertama")
        pihak_2  = doc_ctx.get("pihak_kedua", "Pihak Kedua")
        sektor   = doc_ctx.get("sektor_bisnis", "umum")
        pokok    = doc_ctx.get("pokok_perjanjian", "")

        # ── Whose side are we scoring? (2026-11-11) ───────────────────────
        # The upload modal sends a POSITION ("pertama"/"kedua"), because the
        # literal party names only exist after this metadata pass. Unknown
        # values fall back to the first party so an old client never breaks.
        posisi_raw = (pihak_kita or "pertama").strip().lower()
        pihak_kita_key = "kedua" if posisi_raw == "kedua" else "pertama"
        pihak_kita_nama = pihak_2 if pihak_kita_key == "kedua" else pihak_1
        peran_kita_clean = favorability.normalize_peran(peran_kita)
        print(f"[Compliance] Scoring position for: {pihak_kita_nama} ({peran_kita_clean or 'peran tidak ditentukan'})")

        # ── Compute expiry date ───────────────────────────────────────────
        tanggal_berakhir = None
        if tanggal_pembuatan and durasi_bulan:
            try:
                start_date = datetime.strptime(tanggal_pembuatan, "%Y-%m-%d")
                end_date = start_date + relativedelta(months=int(durasi_bulan))
                tanggal_berakhir = end_date.strftime("%Y-%m-%d")
                print(f"[Compliance] Expiry computed: {tanggal_pembuatan} + {durasi_bulan}mo = {tanggal_berakhir}")
            except Exception as e:
                print(f"[Compliance] Failed to compute expiry: {e}")

        # ── Pass 0D: Explicit end date fallback ───────────────────────────
        if not tanggal_berakhir:
            print("[Compliance] Computation failed/missing, attempting explicit end date extraction...")
            tanggal_berakhir = extract_explicit_end_date(full_text)

        # ── Pass 1: Clause Extraction ─────────────────────────────────────
        print("[Compliance] Pass 1: Extracting clauses & sections...")
        pasal_items = extract_pasal_items(text_snippet, max_items=80, max_chars_per_pasal=4000)

        # Fallback to LLM extractor for non-standard contracts
        if len(pasal_items) < 2:
            print("[Compliance] Regex found <2 clauses, falling back to LLM extractor...")
            pasal_items = llm_extract_pasal_items(full_text, max_items=80)

        # Final fallback: treat whole document as one unit
        if not pasal_items:
            pasal_items = [{"pasal": "Dokumen Keseluruhan", "deskripsi": text_snippet[:1200]}]

        print(f"[Compliance] Found {len(pasal_items)} clauses. Running analysis...")

        # ── Pass 3B (Gap Detector) launched early in parallel with Pass 2 ──
        # gap_detector only depends on pasal_items, doc_ctx, and text_snippet,
        # so starting it now hides its LLM latency behind Pass 2 & Pass 3.
        detected_contract_type = doc_ctx.get("archetype") or "GENERAL"
        missing_clauses: list = []

        def _run_pass3b():
            nonlocal detected_contract_type, missing_clauses
            try:
                from services import gap_detector
                is_bilingual_doc = bool(bilingual_meta and bilingual_meta.get("is_bilingual"))
                detected_contract_type, missing_clauses = gap_detector.detect_missing_clauses(
                    pasal_items=pasal_items,
                    contract_type=detected_contract_type,
                    doc_type=doc_type,
                    pihak_kita=pihak_kita_nama,
                    peran_kita=peran_kita_clean,
                    is_bilingual=is_bilingual_doc,
                    full_text=text_snippet,
                    filename=clean_filename,
                )
                print(f"[Compliance] Gap detector ({detected_contract_type}): {len(missing_clauses)} gap(s) found")
            except Exception as e:
                print(f"[Compliance] Gap detector skipped: {e}")

        gap_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        f3b = gap_pool.submit(_run_pass3b)

        try:
            # ── Pass 2: Per-clause RAG + CrossEncoder Rerank + Batched LLM ───
            COSINE_THRESHOLD = 0.45  # tightened from 0.60 to reduce wrong-domain citations
            # B2: load the per-user exclusion set once (≤5 worker threads × ≤20
            # clauses would otherwise re-read it); uid binds the internal-chunk branch.
            uid = str(current_user["id"])
            excluded = get_user_exclusions(current_user["id"])
            rid = report_id if report_id else None   # B3: '' / None => no narrowing

            from services.scraper.category_classifier import classify_contract_categories
            contract_categories = classify_contract_categories(
                f"{doc_type}\n{sektor}\n{pokok}\n{text_snippet[:6000]}"
            )
            print(f"[Compliance] Category routing: {contract_categories}")

            clauses_to_check = pasal_items[:80]
            # Batch-embed all clause search queries in one forward pass
            from services.embed_service import embed_documents
            clause_queries = [
                f"[{doc_type}] [{sektor}] {(c.get('deskripsi') or '')[:400]}"
                for c in clauses_to_check
            ]
            clause_vecs = embed_documents(clause_queries) if clause_queries else []

            def _retrieve_worker(args):
                idx, (c, vec) = args
                ctx = _retrieve_clause_context(
                    c, doc_type, sektor, COSINE_THRESHOLD, uid, excluded, rid,
                    contract_categories=contract_categories,
                    precomputed_vec=vec,
                )
                return idx, ctx

            retrieved: list[tuple[int, Optional[dict]]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                for item in executor.map(_retrieve_worker, enumerate(zip(clauses_to_check, clause_vecs))):
                    retrieved.append(item)

            ordered_results: dict[int, dict] = {}
            regulated_items: list[tuple[int, dict]] = []
            for idx, ctx in retrieved:
                if ctx is None:
                    continue
                if not ctx["relevant_refs"]:
                    ordered_results[idx] = _unregulated_clause_result(ctx)
                else:
                    regulated_items.append((idx, ctx))

            batch_size = max(1, int(os.environ.get("COMPLIANCE_CLAUSE_BATCH_SIZE", "4")))
            batches = [
                regulated_items[i:i + batch_size]
                for i in range(0, len(regulated_items), batch_size)
            ]

            def _verify_batch_worker(batch: list[tuple[int, dict]]) -> list[tuple[int, dict]]:
                batch_ctxs = [ctx for _, ctx in batch]
                verified = verify_clauses_batch(
                    batch_ctxs, doc_type, pihak_1, pihak_2, sektor, pokok,
                    pihak_kita_nama, peran_kita_clean,
                )
                return [(idx, res) for (idx, _), res in zip(batch, verified)]

            if batches:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    for batch_out in executor.map(_verify_batch_worker, batches):
                        for idx, res in batch_out:
                            ordered_results[idx] = res

            results = [ordered_results[i] for i in sorted(ordered_results)]

            # ── Pass 3: Commercial-position sweep ────────────────────────────
            try:
                filled = favorability.sweep(results, doc_type, pihak_kita_nama, peran_kita_clean)
                print(f"[Compliance] Position sweep filled {filled} clause(s)")
            except Exception as e:
                print(f"[Compliance] Position sweep skipped: {e}")

            f3b.result()
        finally:
            gap_pool.shutdown(wait=True)

        # ── Pass 4: Document-Level Summary ───────────────────────────────
        fatal_count        = sum(1 for r in results if r["status"] == "FATAL")
        beresiko_count     = sum(1 for r in results if r["status"] == "BERESIKO")
        sesuai_count       = sum(1 for r in results if r["status"] == "SESUAI")
        tidak_diatur_count = sum(1 for r in results if r["status"] == "TIDAK DIATUR")

        # Calculate score only against regulated clauses
        regulated_count = len(results) - tidak_diatur_count
        skor = round((sesuai_count / max(regulated_count, 1)) * 100) if regulated_count > 0 else 100

        # Commercial position tally (independent of the compliance score; a
        # lawful clause can still be one-sided).
        menguntungkan_count = sum(1 for r in results if r.get("posisi_perusahaan") == "MENGUNTUNGKAN")
        merugikan_count     = sum(1 for r in results if r.get("posisi_perusahaan") == "MERUGIKAN")
        netral_count        = len(results) - menguntungkan_count - merugikan_count
        skor_posisi = round(((menguntungkan_count - merugikan_count) / max(len(results), 1)) * 100)

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
            "tidak_diatur": tidak_diatur_count,
            "skor_kepatuhan": skor,
            "pihak_kita": pihak_kita_nama,
            "peran_kita": peran_kita_clean,
            "klausul_menguntungkan": menguntungkan_count,
            "klausul_merugikan": merugikan_count,
            "klausul_netral": netral_count,
            "skor_posisi": skor_posisi,
            "detected_contract_type": detected_contract_type,
            "missing_clauses_count": len(missing_clauses),
        }

        print(f"[Compliance] Done. Score: {summary['skor_kepatuhan']}% | "
              f"SESUAI={sesuai_count} BERESIKO={beresiko_count} FATAL={fatal_count} | "
              f"posisi: +{menguntungkan_count} / -{merugikan_count} / ={netral_count}")

        out_payload = {
            "report": results,
            "summary": summary,
            "detected_contract_type": detected_contract_type,
            "missing_clauses": missing_clauses,
        }
        if bilingual_meta and bilingual_meta.get("is_bilingual"):
            from services import bilingual_extractor
            out_payload["layout_mode"] = bilingual_meta.get("layout_mode", "bilingual_parallel")
            out_payload["column_order"] = bilingual_meta.get("column_order")
            out_payload["governing_language"] = bilingual_meta.get("governing_language", "id")
            out_payload["segments"] = bilingual_meta.get("segments", [])
            out_payload["translation_discrepancies"] = bilingual_extractor.find_translation_discrepancies(
                bilingual_meta.get("segments", [])
            )
        return out_payload

    try:
        out_payload = await run_in_threadpool(_run_pipeline)
        matched_ids = {
            sr["regulation_id"]
            for clause in out_payload.get("report") or []
            for sr in clause.get("supporting_regulations") or []
            if sr.get("regulation_id") is not None
        }
        if matched_ids:
            from services.freshness.live_verifier import (
                record_regulation_matches,
                refresh_stale_regulations_background,
            )
            with pg_service.get_conn() as conn:
                stale_ids = record_regulation_matches(conn, matched_ids)
            if stale_ids:
                background_tasks.add_task(refresh_stale_regulations_background, stale_ids)
        return out_payload
    except HTTPException:
        raise
    except Exception as e:
        print(f"Compliance checker unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan internal saat memproses dokumen.")
    finally:
        # Automagically clean up uploaded PKS memory footprint
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

