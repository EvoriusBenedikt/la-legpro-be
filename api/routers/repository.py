from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
import sqlite3, os
from pydantic import BaseModel
import auth

router = APIRouter(prefix="/api", tags=["repository"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")

class AccessGrantRequest(BaseModel):
    target_user_id: int
    expires_in_days: int = 7

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
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    if not os.path.exists(db_path):
        return {"documents": []}
        
    user_id = current_user.get("id")
    role_level = auth.get_role_level(current_user.get("role", "pengguna"))
    
    allowed_klasifikasi = ["Umum"]
    if role_level >= 2:
        allowed_klasifikasi.append("Rahasia")
    if role_level >= 3:
        allowed_klasifikasi.append("Terbatas")
        
    klas_str = ", ".join(f"'{k}'" for k in allowed_klasifikasi)
        
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # Query regulations where klasifikasi is allowed OR explicitly granted
    query = f"""
        SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi 
        FROM regulations 
        WHERE local_path IS NOT NULL AND local_path != ''
        AND status != 'Menunggu Konfirmasi'
        AND (
            klasifikasi IN ({klas_str}) 
            OR id IN (
                SELECT doc_id FROM access_grants 
                WHERE granted_to = ? 
                AND (expires_at IS NULL OR expires_at = '' OR expires_at >= datetime('now'))
            )
        )
    """
    c.execute(query, (user_id,))
    records = c.fetchall()
    
    docs = []
    for row in records:
        # Depending on schema, klasifikasi might be at index 7. Handle safely.
        reg_id, judul, nomor, jenis, sektor, status, local_path = row[:7]
        klasifikasi = row[7] if len(row) > 7 else "Umum"
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
    conn.close()
    
    return {"documents": docs}


@router.get("/repository/pending")
async def get_pending_repository(current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    query = """
        SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi 
        FROM regulations 
        WHERE status = 'Menunggu Konfirmasi'
    """
    c.execute(query)
    records = c.fetchall()
    
    docs = []
    for row in records:
        reg_id, judul, nomor, jenis, sektor, status, local_path = row[:7]
        klasifikasi = row[7] if len(row) > 7 else "Umum"
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
    conn.close()
    
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
        
    import sqlite3
    import uuid
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    c.execute("SELECT klasifikasi, judul FROM regulations WHERE id = ?", (doc_id,))
    doc = c.fetchone()
    if not doc:
        conn.close()
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
        
    klasifikasi = doc[0] if doc[0] else "Umum"
    doc_judul = doc[1] if doc[1] else doc_id
    
    # FR-22: Manajer cannot grant access to Terbatas documents
    if klasifikasi == "Terbatas" and user_level < 3:
        conn.close()
        raise HTTPException(status_code=403, detail="Manajer tidak dapat memberikan akses untuk dokumen Terbatas. Hanya Direktur atau Sekretaris Perusahaan yang berwenang.")
        
    if not req.reason or len(req.reason.strip()) < 5:
        conn.close()
        raise HTTPException(status_code=400, detail="Alasan wajib diisi (minimal 5 karakter).")
        
    grant_id = str(uuid.uuid4())
    c.execute('''INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason, expires_at)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (grant_id, doc_id, current_user["id"], req.granted_to, req.reason, req.expires_at))
    conn.commit()
    conn.close()
    
    # FR-25: Audit log the grant
    from main import log_audit
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
    
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # FR-23: Sekretaris Perusahaan sees everything; others see only what they granted
    if user_level >= 5:  # Sekretaris Perusahaan
        c.execute('''SELECT ag.id, ag.doc_id, ag.granted_by, ag.granted_to, ag.reason, 
                            ag.expires_at, ag.created_at, r.judul, r.klasifikasi
                     FROM access_grants ag
                     LEFT JOIN regulations r ON ag.doc_id = r.id
                     ORDER BY ag.created_at DESC''')
    else:
        c.execute('''SELECT ag.id, ag.doc_id, ag.granted_by, ag.granted_to, ag.reason, 
                            ag.expires_at, ag.created_at, r.judul, r.klasifikasi
                     FROM access_grants ag
                     LEFT JOIN regulations r ON ag.doc_id = r.id
                     WHERE ag.granted_by = ?
                     ORDER BY ag.created_at DESC''', (current_user["id"],))
    
    rows = c.fetchall()
    conn.close()
    return {"grants": [dict(r) for r in rows]}

@router.delete("/repository/grant/{grant_id}")
async def revoke_grant(grant_id: str, current_user: dict = Depends(auth.get_current_user)):
    """FR-23: Sekretaris Perusahaan can revoke any grant."""
    user_level = auth.get_role_level(current_user.get("role", "pengguna"))
    if user_level < 5:  # Only Sekretaris Perusahaan
        raise HTTPException(status_code=403, detail="Hanya Sekretaris Perusahaan yang dapat mencabut pemberian akses.")
    
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    c.execute("SELECT doc_id, granted_to FROM access_grants WHERE id = ?", (grant_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Grant tidak ditemukan.")
    
    c.execute("DELETE FROM access_grants WHERE id = ?", (grant_id,))
    conn.commit()
    conn.close()
    
    from main import log_audit
    log_audit(current_user.get("id", ""), "REVOKE_ACCESS", row[0], f"Akses dicabut dari: {row[1]}")
    return {"message": "Akses berhasil dicabut."}

def process_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    try:
        from pdf_parser import LegalDocumentParser
        parser = LegalDocumentParser()
        full_text = parser.parse_pdf(file_path)
        
        # ── AI Recommendation (FR-4) ──────────────────────────────────────
        messages = [
            {"role": "system", "content": "Anda adalah analis regulasi korporat. Tugas Anda adalah memberikan rekomendasi tingkat kerahasiaan dokumen berdasarkan isinya. Balas hanya dengan satu kata: 'Umum', 'Rahasia', atau 'Terbatas'."},
            {"role": "user", "content": f"Teks dokumen:\n{full_text[:3000]}\n\nBerdasarkan teks ini, rekomendasikan klasifikasi: Umum, Rahasia, atau Terbatas."}
        ]
        
        recommended_klasifikasi = "Umum"
        try:
            from main import call_glm
            raw_content = call_glm(messages, temperature=0.1, timeout=30)
            raw_content = raw_content.lower()
            if "terbatas" in raw_content:
                recommended_klasifikasi = "Terbatas"
            elif "rahasia" in raw_content:
                recommended_klasifikasi = "Rahasia"
        except Exception as llm_err:
            print(f"LLM classification error: {llm_err}")
            
        import sqlite3
        db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
        conn = sqlite3.connect(db_path, timeout=30.0)
        c = conn.cursor()
        c.execute("UPDATE regulations SET status = 'Menunggu Konfirmasi', klasifikasi = ? WHERE id = ?", (recommended_klasifikasi, doc_id))
        conn.commit()
        conn.close()
        print(f"Document {doc_id} set to Pending Confirmation with AI Recommendation: {recommended_klasifikasi}")
        
    except Exception as e:
        print(f"Error in process_document_background: {e}")
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.join(BASE_DIR, "data", "legal_metadata.db"), timeout=30.0)
            c = conn.cursor()
            c.execute("UPDATE regulations SET status = 'Gagal - Error' WHERE id = ?", (doc_id,))
            conn.commit()
            conn.close()
        except:
            pass

def ingest_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    try:
        from pdf_parser import LegalDocumentParser, LegalChunker
        parser = LegalDocumentParser()
        chunker = LegalChunker()
        
        full_text = parser.parse_pdf(file_path)
        
        # Duplicate Detection (FR-5) 
        fingerprint_text = full_text[:1500]
        from main import get_chroma_collection
        collection = get_chroma_collection()
        dup_results = collection.query(
            query_texts=[fingerprint_text],
            n_results=1
        )
        
        import sqlite3
        import os
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
        conn = sqlite3.connect(db_path, timeout=30.0)
        c = conn.cursor()
        
        if dup_results and dup_results['distances'] and len(dup_results['distances'][0]) > 0:
            dist = dup_results['distances'][0][0]
            if dist < 0.15:
                # Cleanup temp file
                if os.path.exists(file_path):
                    os.remove(file_path)
                c.execute("UPDATE regulations SET status = 'Gagal - Duplikat' WHERE id = ?", (doc_id,))
                conn.commit()
                conn.close()
                return

        print(f"File saved to DB. Now parsing and embedding: {filename}")
        
        # Contextual Enrichment - Generate Global Document Summary
        document_summary = ""
        try:
            from main import call_glm
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
            collection = get_chroma_collection()
            collection.add(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            print(f"Successfully added {len(documents)} chunks to ChromaDB!")
            
            # Sparse Retrieval Injection (BM25 FTS5)
            try:
                fts_records = []
                for idx, c_data in zip(ids, chunks):
                    w_ctx = c_data["metadata"].get("window_context", "")
                    fts_records.append((idx, doc_id, c_data["text"], w_ctx))
                c.executemany("INSERT INTO chunks_fts (chunk_id, doc_id, text, window_context) VALUES (?, ?, ?, ?)", fts_records)
            except Exception as e:
                print(f"Failed to inject into chunks_fts: {e}")
        
        c.execute("UPDATE regulations SET status = 'Berlaku' WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()

        # Knowledge Graph Extraction (real-time, FR-KG)
        judul = filename.replace('.pdf', '')
        try:
            from main import extract_and_store_graph
            extract_and_store_graph(doc_id, full_text, nomor, judul, jenis)
        except Exception as kg_err:
            print(f"[KG] Non-fatal extraction error for {nomor}: {kg_err}")
        return
        
    except Exception as e:
        print(f"Error processing PDF: {e}")
        try:
            import sqlite3
            import os
            BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            conn = sqlite3.connect(os.path.join(BASE_DIR, "data", "legal_metadata.db"), timeout=30.0)
            c = conn.cursor()
            c.execute("UPDATE regulations SET status = 'Gagal - Error' WHERE id = ?", (doc_id,))
            conn.commit()
            conn.close()
        except:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Knowledge Graph Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.delete("/repository/document/{doc_id}")
async def delete_document(doc_id: str, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    """Deletes a document from the repository."""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    # Check if doc exists
    c.execute("SELECT local_path, judul FROM regulations WHERE id = ?", (doc_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Not Found")
        
    local_path, judul = row
    
    # 1. Delete physical file
    if local_path and os.path.exists(local_path):
        try:
            os.remove(local_path)
        except Exception as e:
            print(f"Error deleting file {local_path}: {e}")
            
    # 2. Delete from ChromaDB & FTS
    try:
        from main import get_chroma_collection
        collection = get_chroma_collection()
        collection.delete(where={"reg_id": doc_id})
        
        c.execute("DELETE FROM chunks_fts WHERE doc_id = ?", (doc_id,))
    except Exception as e:
        print(f"Error deleting from ChromaDB/FTS: {e}")
        
    # 3. Delete from Knowledge Graph
    try:
        c.execute("DELETE FROM kg_nodes WHERE doc_id = ?", (doc_id,))
        c.execute("DELETE FROM kg_edges WHERE source_doc_id = ? OR target_doc_id = ?", (doc_id, doc_id))
    except Exception as e:
        print(f"Error deleting from KG: {e}")
        
    # 4. Delete Access Grants
    try:
        c.execute("DELETE FROM access_grants WHERE doc_id = ?", (doc_id,))
    except Exception as e:
        pass
        
    # 5. Delete Document Record
    c.execute("DELETE FROM regulations WHERE id = ?", (doc_id,))
    
    conn.commit()
    conn.close()
    
    # 6. Audit Log
    from main import log_audit
    log_audit(current_user.get("id", ""), "DELETE_DOCUMENT", doc_id, f"Menghapus dokumen: {judul}")
 
    return {"message": "Dokumen berhasil dihapus."}
