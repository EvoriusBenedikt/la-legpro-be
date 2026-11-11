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
from services.llm_client import call_glm, extract_json

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
        # call_glm is blocking `requests` code; awaited in a worker thread so
        # the single-threaded event loop keeps serving everyone else while the
        # LLM call runs (previously froze the whole backend for the call's
        # duration — healthz included; bug_reports.md 2026-10-07).
        raw_content = await run_in_threadpool(call_glm, messages_llm, temperature=0.1, timeout=60)
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
            call_glm,
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


def process_single_pasal(c, doc_type, pihak_1, pihak_2, sektor, pokok, COSINE_THRESHOLD, uid, excluded, report_id,
                         pihak_kita_nama="", peran_kita=None):
    pasal_label = c.get("pasal", "Pasal")
    desc = c.get("deskripsi", "")
    if not desc or len(desc) < 10:
        return None

    # Map doc_type to doc_category
    doc_cat = "NDA" if "NDA" in str(doc_type).upper() or "NON-DISCLOSURE" in str(doc_type).upper() else "PKS"

    # Enrich the search query with document context for better RAG retrieval
    search_query = f"[{doc_type}] [{sektor}] {desc[:400]}"
    from services.embed_service import embed_query
    query_vec = embed_query(search_query)
    with pg_service.get_conn() as conn:
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        # B2: also admit the caller's OWN internal uploads (doc_category NULL,
        # so they never matched before). Additive: it can only add the user's
        # private internal rows to the candidate pool; the < 0.45 distance gate
        # and is_regulation_relevant micro-check still reject irrelevant ones.
        # shortcut: keep LIMIT 3 — shared across public + admitted internal
        # candidates, so a user with many internal chunks could crowd out public
        # law for a clause (moot today: no FE path produces internal chunks).
        # Revisit raising it only if internal docs crowd out public law in testing.
        cur = conn.execute(
            "SELECT id, doc_id, text, jenis, nomor, sektor, "
            "embedding <=> %s::vector AS dist "
            "FROM chunks "
            "WHERE embedding IS NOT NULL "
            "AND ( doc_category = ANY(%s) "
            "      OR (visibility = 'private' AND user_id = %s::text "
            "          AND doc_id IS NULL AND id LIKE 'internal\\_%%' "
            "          AND (report_id IS NULL OR report_id = %s)) ) "
            "ORDER BY embedding <=> %s::vector LIMIT 3",
            (query_vec, [doc_cat, "UMUM"], uid, report_id, query_vec))
        sr_rows = cur.fetchall()

    # B2: drop deselected sources before the distance/relevance loop.
    from services.rag_service import chunk_source_ref
    sr_rows = [r for r in sr_rows
               if chunk_source_ref(r["doc_id"], r["id"]) not in excluded]

    supporting_regulations = []
    relevant_refs = []

    if sr_rows:
        from services.rag_service import is_regulation_relevant
        for row in sr_rows:
            dist = float(row["dist"])
            if dist < COSINE_THRESHOLD:
                doc_text = row["text"]
                reg_name = f"{row['jenis'] or 'Aturan'} Nomor {row['nomor'] or 'N/A'}"

                # Relevance confirmation micro-call
                if is_regulation_relevant(desc, doc_text, reg_name):
                    supporting_regulations.append({
                        "jenis": row['jenis'] or 'Aturan',
                        "nomor": row['nomor'] or 'N/A',
                        "sektor": row['sektor'] or 'Umum',
                        "teks": doc_text
                    })
                    relevant_refs.append(f"--- {reg_name} ---\n{doc_text[:700]}")
                else:
                    print(f"  [Relevance] {reg_name} rejected for {pasal_label}")

    if not relevant_refs:
        # No regulation matched: no LLM call here, so the commercial-position
        # fields start neutral and are filled later by the batched sweep
        # (favorability.sweep) — otherwise price/term/liability clauses, the
        # ones where "is this good for us?" matters most, would stay blank.
        return {
            "pasal": pasal_label,
            "isi_pasal": desc,
            "status": "TIDAK DIATUR",
            "penjelasan": "Tidak ditemukan regulasi yang secara langsung relevan dengan klausul ini dalam database saat ini.",
            "rekomendasi": "Pastikan klausul ini wajar secara keperdataan dan tidak bertentangan dengan KUH Perdata.",
            "ai_analysis": "",
            "supporting_regulations": supporting_regulations,
            **favorability.empty_position(),
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

        # Commercial position is a SECOND axis (2026-11-11): instruction 3 stays
        # untouched so `penjelasan` remains comparable with reports analyzed
        # before this feature; the plus/minus verdict is additive.
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
        raw_v = call_glm(
            [{"role": "user", "content": prompt_verify}],
            temperature=0.1,
            timeout=90
        )
        ans = extract_json(raw_v)

        status_raw = str(ans.get("status", "BERESIKO")).upper()
        if "SESUAI" in status_raw:       status_final = "SESUAI"
        elif "FATAL" in status_raw:       status_final = "FATAL"
        elif "TIDAK DIATUR" in status_raw: status_final = "TIDAK DIATUR"
        else:                              status_final = "BERESIKO"

        return favorability.apply_position({
            "pasal": pasal_label,
            "isi_pasal": desc,
            "status": status_final,
            "pasal_regulasi_terkait": ans.get("pasal_regulasi_terkait", ""),
            "penjelasan": ans.get("penjelasan", "Gagal diverifikasi"),
            "rekomendasi": ans.get("rekomendasi", ""),
            "ai_analysis": ans.get("ai_analysis", ""),
            "supporting_regulations": supporting_regulations
        }, ans)

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
    from services.llm_client import call_glm_vision, VLM_PAGE_PROMPT, vlm_extract_page_image
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
                            txt = call_glm_vision(b64_data, VLM_PAGE_PROMPT, timeout=75)
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
                    ocr_txt = call_glm_vision(b64_img, VLM_PAGE_PROMPT, timeout=75)
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
    from services.llm_client import call_glm_vision, VLM_PAGE_PROMPT
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
        vlm_text = call_glm_vision(img_b64, VLM_PAGE_PROMPT, timeout=60, mime_type=mime)
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
        full_text = extract_text_multi_format(temp_path, file.filename, use_ocr=is_ocr)
        text_snippet = full_text[:250000]

        if len(text_snippet.strip()) < 100:
            raise HTTPException(
                status_code=400,
                detail="Dokumen tidak dapat dibaca atau teks kurang dari 100 karakter. Jika dokumen berupa pindaian (scan) atau gambar dalam DOCX/PDF, pastikan gambar terbaca jelas atau aktifkan opsi OCR."
            )

        # ── Pass 0A: Document Metadata Understanding ──────────────────────
        print("[Compliance] Pass 0A: Extracting document metadata...")
        doc_ctx  = understand_document(full_text)
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

        # ── Pass 0B: Dedicated signing date extraction ────────────────────
        print("[Compliance] Pass 0B: Extracting signing date...")
        tanggal_pembuatan = extract_signing_date(full_text)

        # ── Pass 0C: Dedicated duration extraction ────────────────────────
        print("[Compliance] Pass 0C: Extracting contract duration...")
        durasi_bulan = extract_duration_months(full_text)

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

        # ── Pass 2: Per-clause RAG + LLM Compliance Check ────────────────
        COSINE_THRESHOLD = 0.45  # tightened from 0.60 to reduce wrong-domain citations
        # B2: load the per-user exclusion set once (≤5 worker threads × ≤20
        # clauses would otherwise re-read it); uid binds the internal-chunk branch.
        uid = str(current_user["id"])
        excluded = get_user_exclusions(current_user["id"])
        rid = report_id if report_id else None   # B3: '' / None => no narrowing

        def worker(c):
            return process_single_pasal(c, doc_type, pihak_1, pihak_2, sektor, pokok,
                                        COSINE_THRESHOLD, uid, excluded, rid,
                                        pihak_kita_nama, peran_kita_clean)

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for res in executor.map(worker, pasal_items[:80]):
                if res:
                    results.append(res)

        # ── Pass 3: Commercial-position sweep ────────────────────────────
        # TIDAK DIATUR clauses return before any LLM call, so they carry
        # neutral placeholders. Fill them in batches; a failure here must never
        # fail the analysis, so it is logged and swallowed.
        try:
            filled = favorability.sweep(results, doc_type, pihak_kita_nama, peran_kita_clean)
            print(f"[Compliance] Position sweep filled {filled} clause(s)")
        except Exception as e:
            print(f"[Compliance] Position sweep skipped: {e}")

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
        }

        print(f"[Compliance] Done. Score: {summary['skor_kepatuhan']}% | "
              f"SESUAI={sesuai_count} BERESIKO={beresiko_count} FATAL={fatal_count} | "
              f"posisi: +{menguntungkan_count} / -{merugikan_count} / ={netral_count}")

        return {"report": results, "summary": summary}

    try:
        return await run_in_threadpool(_run_pipeline)
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

