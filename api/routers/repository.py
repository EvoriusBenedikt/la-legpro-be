from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
import os
from pydantic import BaseModel
from typing import Optional
import auth
from services import pg_service

router = APIRouter(prefix="/api", tags=["repository"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")

class AccessGrantRequest(BaseModel):
    granted_to: str
    reason: str
    expires_at: Optional[str] = None

@router.get("/pdf/{filename}")
async def serve_pdf(filename: str):
    """
    Return PDF bytes encoded as base64 JSON.
    IDM cannot intercept this because the Content-Type is application/json, not application/pdf.
    The frontend decodes the base64 and creates a blob:// URL to render in an iframe.
    """
    import base64
    safe_filename = os.path.basename(filename.replace("\\", "/"))  # prevent path traversal
    file_path = os.path.join(PDFS_DIR, safe_filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"PDF '{safe_filename}' not found")
    with open(file_path, "rb") as f:
        pdf_bytes = f.read()
    return {"filename": safe_filename, "data": base64.b64encode(pdf_bytes).decode("utf-8")}

@router.get("/repository")
async def get_repository(current_user: dict = Depends(auth.get_current_user)):
    # FR-24: Admin cannot access document repository
    if current_user.get("role", "pengguna").lower() == "admin":
        raise HTTPException(status_code=403, detail="Admin sistem tidak memiliki kewenangan untuk mengakses repositori dokumen.")
    # (Migration M3: the SQLite file-existence short-circuit is gone -- PG is
    # the only store now, and connectivity failures raise loudly.)
        
    user_id = current_user.get("id")
    role_level = auth.get_role_level(current_user.get("role", "pengguna"))
    
    allowed_klasifikasi = ["Umum"]
    if role_level >= 2:
        allowed_klasifikasi.append("Rahasia")
    if role_level >= 3:
        allowed_klasifikasi.append("Terbatas")
        
    # (Migration M3: the klasifikasi filter now uses ANY(%s) -- no inline
    # string building)
    
    # Query regulations where klasifikasi is allowed OR explicitly granted
    # (Migration M3: ANY(%s) replaces the inline IN-list; access_grants.doc_id
    # is TEXT while regulations.id is int -> id::text comparison.)
    records = pg_service.query(
        "SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi "
        "FROM regulations "
        "WHERE local_path IS NOT NULL AND local_path != '' "
        "AND status != 'Menunggu Konfirmasi' "
        "AND ( "
        "    klasifikasi = ANY(%s) "
        "    OR id::text IN ( "
        "        SELECT doc_id FROM access_grants "
        "        WHERE granted_to = %s "
        "        AND (expires_at IS NULL OR expires_at >= NOW()) "
        "    ) "
        ")",
        (allowed_klasifikasi, user_id))
    
    docs = []
    for row in records:
        reg_id = row["id"]
        judul = row["judul"]
        nomor = row["nomor"]
        jenis = row["jenis"]
        sektor = row["sektor"]
        status = row["status"]
        local_path = row["local_path"]
        klasifikasi = row["klasifikasi"]
        filename = os.path.basename(local_path) if local_path else None
        docs.append({
            "id": str(reg_id) if reg_id is not None else None,
            "judul": str(judul) if judul else "",
            "nomor": str(nomor) if nomor is not None else "",
            "jenis": str(jenis) if jenis else "",
            "sektor": str(sektor) if sektor else "",
            "status": str(status) if status else "",
            "klasifikasi": str(klasifikasi) if klasifikasi else "Umum",
            "filename": filename
        })
    return {"documents": docs}


@router.get("/repository/pending")
async def get_pending_repository(current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    records = pg_service.query(
        "SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi "
        "FROM regulations "
        "WHERE status = 'Menunggu Konfirmasi'")
    
    docs = []
    for row in records:
        reg_id = row["id"]
        judul = row["judul"]
        nomor = row["nomor"]
        jenis = row["jenis"]
        sektor = row["sektor"]
        status = row["status"]
        local_path = row["local_path"]
        klasifikasi = row["klasifikasi"]
        filename = os.path.basename(local_path) if local_path else None
        docs.append({
            "id": str(reg_id) if reg_id is not None else None,
            "judul": str(judul) if judul else "",
            "nomor": str(nomor) if nomor is not None else "",
            "jenis": str(jenis) if jenis else "",
            "sektor": str(sektor) if sektor else "",
            "status": str(status) if status else "",
            "klasifikasi": str(klasifikasi) if klasifikasi else "Umum",
            "filename": filename
        })
    return {"documents": docs}

@router.post("/documents/{doc_id}/grant-access")
async def grant_document_access(
    doc_id: str,
    req: AccessGrantRequest,
    current_user: dict = Depends(auth.get_current_user)
):
    user_role = current_user.get("role", "pengguna").lower()
    user_level = auth.get_role_level(user_role)
    # FR-21 & FR-22: Only Manajer (level 2+) can grant access
    if user_level < 2:
        raise HTTPException(status_code=403, detail="Hanya Manajer, Direktur, atau Sekretaris Perusahaan yang dapat memberikan akses.")
        
    import uuid
    
    # (Migration M3: id::text -- doc_id arrives as a URL string; non-numeric
    # ids match nothing, exactly like SQLite's type affinity did.)
    doc = pg_service.query_one(
        "SELECT klasifikasi, judul FROM regulations WHERE id::text = %s", (doc_id,))
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
        
    klasifikasi = doc["klasifikasi"] if doc["klasifikasi"] else "Umum"
    doc_judul = doc["judul"] if doc["judul"] else doc_id
    
    # FR-22: Manajer cannot grant access to Terbatas documents
    if klasifikasi == "Terbatas" and user_level < 3:
        raise HTTPException(status_code=403, detail="Manajer tidak dapat memberikan akses untuk dokumen Terbatas. Hanya Direktur atau Sekretaris Perusahaan yang berwenang.")
        
    if not req.reason or len(req.reason.strip()) < 5:
        raise HTTPException(status_code=400, detail="Alasan wajib diisi (minimal 5 karakter).")
        
    grant_id = str(uuid.uuid4())
    # expires_at: '' (legacy empty string) becomes NULL -- TIMESTAMPTZ cannot
    # store empty strings.
    pg_service.execute(
        "INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (grant_id, doc_id, current_user["id"], req.granted_to, req.reason, req.expires_at or None))
    
    # FR-25: Audit log the grant
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "GRANT_ACCESS", doc_id, 
              f"Diberikan kepada: {req.granted_to}, Dokumen: {doc_judul}, Alasan: {req.reason}")
    
    return {"message": "Akses berhasil diberikan."}

@router.get("/repository/grants")
async def get_all_grants(current_user: dict = Depends(auth.get_current_user)):
    """FR-23: Sekretaris Perusahaan can see all grants; others see only their own."""
    user_role = current_user.get("role", "pengguna").lower()
    user_level = auth.get_role_level(user_role)
    if user_level < 2:
        raise HTTPException(status_code=403, detail="Akses ditolak.")
    
    # (Migration M3: to_char keeps the legacy TEXT timestamp format for the FE;
    # the join needs r.id::text because ag.doc_id is TEXT.)
    base_sql = (
        "SELECT ag.id, ag.doc_id, ag.granted_by, ag.granted_to, ag.reason, "
        "to_char(ag.expires_at, 'YYYY-MM-DD HH24:MI:SS') AS expires_at, "
        "to_char(ag.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at, "
        "r.judul, r.klasifikasi "
        "FROM access_grants ag "
        "LEFT JOIN regulations r ON ag.doc_id = r.id::text ")
    
    # FR-23: Sekretaris Perusahaan sees everything; others see only what they granted
    if user_level >= 5:  # Sekretaris Perusahaan
        rows = pg_service.query(base_sql + "ORDER BY ag.created_at DESC")
    else:
        rows = pg_service.query(
            base_sql + "WHERE ag.granted_by = %s ORDER BY ag.created_at DESC",
            (current_user["id"],))
    
    return {"grants": rows}

@router.delete("/repository/grant/{grant_id}")
async def revoke_grant(grant_id: str, current_user: dict = Depends(auth.get_current_user)):
    """FR-23: Sekretaris Perusahaan can revoke any grant."""
    user_level = auth.get_role_level(current_user.get("role", "pengguna"))
    if user_level < 5:  # Only Sekretaris Perusahaan
        raise HTTPException(status_code=403, detail="Hanya Sekretaris Perusahaan yang dapat mencabut pemberian akses.")
    
    row = pg_service.query_one(
        "SELECT doc_id, granted_to FROM access_grants WHERE id = %s", (grant_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Grant tidak ditemukan.")
    
    pg_service.execute("DELETE FROM access_grants WHERE id = %s", (grant_id,))
    
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "REVOKE_ACCESS", row["doc_id"], f"Akses dicabut dari: {row['granted_to']}")
    return {"message": "Akses berhasil dicabut."}

def process_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    from services.app_state import ACTIVE_TASKS
    from datetime import datetime
    task_id = f"task_{doc_id}"
    ACTIVE_TASKS[task_id] = {"name": f"Parsing (AI Review): {filename}", "status": "RUNNING", "start_time": datetime.now(), "end_time": None}
    try:
        # (R1 bugfix: hybrid PyMuPDF+VLM extraction replaces the dead
        # PaddleOCR chain; see la-legpro-doc/bug_reports.md.)
        from services.rag_service import extract_text_hybrid
        full_text = extract_text_hybrid(file_path)
        
        # ── AI Recommendation (FR-4) ──────────────────────────────────────
        messages = [
            {"role": "system", "content": "Anda adalah analis regulasi korporat. Tugas Anda adalah memberikan rekomendasi tingkat kerahasiaan dokumen berdasarkan isinya. Balas hanya dengan satu kata: 'Umum', 'Rahasia', atau 'Terbatas'."},
            {"role": "user", "content": f"Teks dokumen:\n{full_text[:3000]}\n\nBerdasarkan teks ini, rekomendasikan klasifikasi: Umum, Rahasia, atau Terbatas."}
        ]
        
        recommended_klasifikasi = "Umum"
        try:
            from services.llm_client import call_glm
            raw_content = call_glm(messages, temperature=0.1, timeout=30)
            raw_content = raw_content.lower()
            if "terbatas" in raw_content:
                recommended_klasifikasi = "Terbatas"
            elif "rahasia" in raw_content:
                recommended_klasifikasi = "Rahasia"
        except Exception as llm_err:
            print(f"LLM classification error: {llm_err}")
            
        pg_service.execute(
            "UPDATE regulations SET status = 'Menunggu Konfirmasi', klasifikasi = %s "
            "WHERE id::text = %s",
            (recommended_klasifikasi, doc_id))
        print(f"Document {doc_id} set to Pending Confirmation with AI Recommendation: {recommended_klasifikasi}")
        ACTIVE_TASKS[task_id]["status"] = "COMPLETED"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now()
        
    except Exception as e:
        ACTIVE_TASKS[task_id]["status"] = "FAILED"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now()
        print(f"Error in process_document_background: {e}")
        try:
            pg_service.execute(
                "UPDATE regulations SET status = 'Gagal - Error' WHERE id::text = %s", (doc_id,))
        except:
            pass

def ingest_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    from services.app_state import ACTIVE_TASKS
    from datetime import datetime
    task_id = f"task_{doc_id}"
    ACTIVE_TASKS[task_id] = {"name": f"Ingesting (Vector+KG): {filename}", "status": "RUNNING", "start_time": datetime.now(), "end_time": None}
    try:
        from pdf_parser import LegalChunker
        from services.rag_service import extract_text_hybrid
        chunker = LegalChunker()
        
        # (R1 bugfix: hybrid PyMuPDF+VLM extraction replaces the dead
        # PaddleOCR chain; see la-legpro-doc/bug_reports.md.)
        full_text = extract_text_hybrid(file_path)
        
        # Duplicate Detection (FR-5)
        # (Migration M3: pgvector cosine distance `<=>` replaces the Chroma
        # query -- same cosine metric, same 0.15 threshold.)
        fingerprint_text = full_text[:1500]
        from services.embed_service import embed_query
        fp_vec = embed_query(fingerprint_text)
        with pg_service.get_conn() as conn_dup:
            conn_dup.execute("SET LOCAL hnsw.ef_search = 100")
            cur_dup = conn_dup.execute(
                "SELECT embedding <=> %s::vector AS dist FROM chunks "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT 1",
                (fp_vec, fp_vec))
            dup_row = cur_dup.fetchone()
        
        import os
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        if dup_row and dup_row["dist"] is not None:
            dist = dup_row["dist"]
            if dist < 0.15:
                # Cleanup temp file
                if os.path.exists(file_path):
                    os.remove(file_path)
                pg_service.execute(
                    "UPDATE regulations SET status = 'Gagal - Duplikat' WHERE id::text = %s",
                    (doc_id,))
                ACTIVE_TASKS[task_id]["status"] = "FAILED"
                ACTIVE_TASKS[task_id]["end_time"] = datetime.now()
                return

        print(f"File saved to DB. Now parsing and embedding: {filename}")
        
        # Contextual Enrichment - Generate Global Document Summary
        document_summary = ""
        try:
            from services.llm_client import call_glm
            summary_prompt = (
                "Buatlah ringkasan singkat (maksimal 2 kalimat) yang menjelaskan tentang apa dokumen ini, "
                "siapa pihak yang terlibat, dan apa topik utamanya. "
                "Tujuan ringkasan ini adalah untuk memberikan konteks global pada potongan-potongan kecil teks dokumen.\\n\\n"
                f"TEKS DOKUMEN (Bagian Awal):\\n{full_text[:4000]}"
            )
            document_summary = call_glm([{"role": "user", "content": summary_prompt}], temperature=0.1, timeout=30)
            print(f"Generated contextual summary: {document_summary}")
        except Exception as e:
            print(f"Warning: Failed to generate document summary: {e}")

        # Vector DB Injection
        base_metadata = {
            "reg_id": doc_id,
            "judul": filename.replace('.pdf', ''),
            "nomor": nomor,
            "jenis": jenis,
            "sektor": sektor,
            "status": status
        }
        
        chunks = chunker.chunk_document(full_text, base_metadata, document_summary=document_summary)
        
        documents = []
        metadatas = []
        ids = []
        
        import hashlib
        for i, c_data in enumerate(chunks):
            clean_metadata = {k: v for k, v in c_data["metadata"].items() if v is not None}
            chunk_id = f"{nomor}_chunk_{i}"
            hash_id = hashlib.md5(chunk_id.encode('utf-8')).hexdigest()
            
            documents.append(c_data["text"])
            metadatas.append(clean_metadata)
            ids.append(hash_id)
            
        if documents:
            # (Migration M3: one unified chunks-table insert replaces BOTH the
            # Chroma collection.add and the chunks_fts injection below -- the
            # row carries the embedding (dense half) and sparse_legacy=TRUE
            # marks it for the PG full-text index (sparse half), exactly like
            # the legacy dual write.)
            from services.embed_service import embed_documents
            embeddings = embed_documents(documents)
            with pg_service.get_conn() as conn_ins:
                for chunk_id, text, meta, emb in zip(ids, documents, metadatas, embeddings):
                    conn_ins.execute(
                        "INSERT INTO chunks (id, doc_id, text, window_context, domain, "
                        "jenis, judul, nomor, sektor, status, filename, doc_category, "
                        "visibility, user_id, embedding, sparse_legacy) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s::vector, TRUE) "
                        "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                        "window_context = EXCLUDED.window_context, "
                        "embedding = EXCLUDED.embedding",
                        (chunk_id, meta.get("reg_id"), text, meta.get("window_context"),
                         meta.get("domain"), meta.get("jenis"), meta.get("judul"),
                         meta.get("nomor"), meta.get("sektor"), meta.get("status"),
                         meta.get("filename"), meta.get("doc_category"),
                         meta.get("visibility"), meta.get("user_id"), emb))
            print(f"Successfully added {len(documents)} chunks to the PG store!")
            
            # (the legacy chunks_fts injection is gone -- sparse_legacy=TRUE on
            # the rows above already puts them in the PG full-text index)
        
        pg_service.execute("UPDATE regulations SET status = 'Berlaku' WHERE id::text = %s", (doc_id,))

        # Knowledge Graph Extraction (real-time, FR-KG)
        judul = filename.replace('.pdf', '')
        try:
            from services.kg_service import extract_and_store_graph
            extract_and_store_graph(doc_id, full_text, nomor, judul, jenis)
        except Exception as kg_err:
            print(f"[KG] Non-fatal extraction error for {nomor}: {kg_err}")
            
        ACTIVE_TASKS[task_id]["status"] = "COMPLETED"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now()
        return
        
    except Exception as e:
        ACTIVE_TASKS[task_id]["status"] = "FAILED"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now()
        print(f"Error processing PDF: {e}")
        try:
            pg_service.execute(
                "UPDATE regulations SET status = 'Gagal - Error' WHERE id::text = %s", (doc_id,))
        except:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Knowledge Graph Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.delete("/repository/document/{doc_id}")
async def delete_document(doc_id: str, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    """Deletes a document from the repository."""
    
    # Check if doc exists
    row = pg_service.query_one(
        "SELECT local_path, judul FROM regulations WHERE id::text = %s", (doc_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    # (M4 hardening: refuse the delete while this document's background task
    # (AI parse or vector+KG ingest) is still RUNNING. Legacy let the delete
    # race the ingest: the ingest's later INSERTs recreated chunks/KG rows for
    # the already-deleted doc, leaving orphans in the vector index. Observed
    # live during the M3 smokes (doc 983 -> 8 orphan chunks); see the
    # bug_reports.md entry "Deleting a document while its background ingest is
    # running". The tiny window between confirm returning and the task thread
    # registering itself remains -- the guard shrinks it from ~20s to ~ms.)
    from services.app_state import ACTIVE_TASKS
    task = ACTIVE_TASKS.get(f"task_{doc_id}")
    if task and task.get("status") == "RUNNING":
        raise HTTPException(
            status_code=409,
            detail="Dokumen sedang diproses (ingest berjalan). Coba hapus lagi beberapa saat.")

    local_path, judul = row["local_path"], row["judul"]
    
    # 1. Delete physical file
    if local_path and os.path.exists(local_path):
        try:
            os.remove(local_path)
        except Exception as e:
            print(f"Error deleting file {local_path}: {e}")
            
    # 2-5. Delete chunks + KG + grants + the regulation row in ONE transaction
    # (Migration M3: the unified chunks table replaces both the Chroma delete
    # and the chunks_fts delete. NOTE: the legacy kg_edges cleanup referenced
    # source_doc_id/target_doc_id -- columns that do not exist in the live
    # schema -- so it always failed silently and left orphaned edges; this
    # now deletes by kg_edges.doc_id, the column that actually exists. The
    # legacy code also committed steps 2-5 together at the end, so a single
    # transaction preserves that all-or-nothing behavior.)
    try:
        with pg_service.get_conn() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
            conn.execute("DELETE FROM kg_edges WHERE doc_id = %s", (doc_id,))
            conn.execute("DELETE FROM kg_nodes WHERE doc_id = %s", (doc_id,))
            conn.execute("DELETE FROM access_grants WHERE doc_id = %s", (doc_id,))
            conn.execute("DELETE FROM regulations WHERE id::text = %s", (doc_id,))
    except Exception as e:
        print(f"Error during document deletion cascade: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
    # (steps 3-4 were folded into the single transaction above)
        
    # (step 5 likewise -- the regulation row is deleted in the transaction above)
    
    # 6. Audit Log
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "DELETE_DOCUMENT", doc_id, f"Menghapus dokumen: {judul}")
 
    return {"message": "Dokumen berhasil dihapus."}

# ─────────────────────────────────────────────────────────────────────────────
# Pending confirmation & failed-document cleanup endpoints
# (moved verbatim from api/main.py during the Phase 2 refactor — same paths,
#  same auth; /api prefix comes from this router)
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmPendingRequest(BaseModel):
    klasifikasi: str

@router.post("/repository/pending/{doc_id}/confirm")
async def confirm_pending_document(
    doc_id: str,
    req: ConfirmPendingRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))
):
    doc = pg_service.query_one(
        "SELECT local_path, nomor, judul, jenis, sektor FROM regulations "
        "WHERE id::text = %s AND status = 'Menunggu Konfirmasi'", (doc_id,))
    
    if not doc:
        raise HTTPException(status_code=404, detail="Dokumen pending tidak ditemukan.")

    local_path, nomor, judul, jenis, sektor = (doc["local_path"], doc["nomor"],
                                               doc["judul"], doc["jenis"], doc["sektor"])
    
    # Update DB
    pg_service.execute(
        "UPDATE regulations SET status = 'Berlaku', klasifikasi = %s WHERE id::text = %s",
        (req.klasifikasi, doc_id))
    
    # Trigger vector DB injection and KG asynchronously
    filename = os.path.basename(local_path)
    background_tasks.add_task(ingest_document_background, local_path, doc_id, filename, nomor, jenis, sektor, 'Berlaku', req.klasifikasi)
    
    return {"message": "Dokumen berhasil dikonfirmasi dan dimasukkan ke repositori."}

@router.delete("/repository/failed")
async def delete_failed_documents(current_user: dict = Depends(auth.require_role("manajer"))):
    """Deletes all documents that failed processing (e.g., Duplicates)."""
    # (no params passed -> psycopg performs no %-substitution; LIKE 'Gagal%'
    # is safe as a literal)
    failed_docs = pg_service.query(
        "SELECT id, local_path FROM regulations WHERE status LIKE 'Gagal%'")
    
    deleted_count = 0
    # Single transaction for all deletes (Migration M3); the file removals
    # stay interleaved exactly like the legacy loop.
    with pg_service.get_conn() as conn:
        for doc in failed_docs:
            doc_id, local_path = doc["id"], doc["local_path"]
            # Delete physical file
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception as e:
                    print(f"Error deleting file {local_path}: {e}")
            # Delete from DB
            conn.execute("DELETE FROM regulations WHERE id = %s", (doc_id,))
            deleted_count += 1
    
    return {"message": f"Berhasil menghapus {deleted_count} dokumen yang gagal."}
