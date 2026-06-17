import re
import os

filepath = os.path.join("api", "main.py")
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Modify get_repository query to exclude 'Menunggu Konfirmasi'
old_repo_query = """        WHERE local_path IS NOT NULL AND local_path != ''
        AND (
            klasifikasi IN ({klas_str}) """
new_repo_query = """        WHERE local_path IS NOT NULL AND local_path != ''
        AND status != 'Menunggu Konfirmasi'
        AND (
            klasifikasi IN ({klas_str}) """
content = content.replace(old_repo_query, new_repo_query)

# 2. Add New Endpoint GET /api/repository/pending
pending_get_endpoint = """
@app.get("/api/repository/pending")
async def get_pending_repository(current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    query = \"\"\"
        SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi 
        FROM regulations 
        WHERE status = 'Menunggu Konfirmasi'
    \"\"\"
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
"""
if "/api/repository/pending" not in content:
    content = content.replace("@app.post(\"/api/documents/{doc_id}/grant-access\")", pending_get_endpoint + "\n@app.post(\"/api/documents/{doc_id}/grant-access\")")

# 3. Add New Endpoint POST /api/repository/pending/{doc_id}/confirm
class_def = """class AccessGrantRequest(BaseModel):"""
confirm_endpoint_and_class = """class ConfirmPendingRequest(BaseModel):
    klasifikasi: str

@app.post("/api/repository/pending/{doc_id}/confirm")
async def confirm_pending_document(
    doc_id: str,
    req: ConfirmPendingRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))
):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    c.execute("SELECT local_path, nomor, judul, jenis, sektor FROM regulations WHERE id = ? AND status = 'Menunggu Konfirmasi'", (doc_id,))
    doc = c.fetchone()
    
    if not doc:
        conn.close()
        raise HTTPException(status_code=404, detail="Dokumen pending tidak ditemukan.")
        
    local_path, nomor, judul, jenis, sektor = doc
    
    # Update DB
    c.execute("UPDATE regulations SET status = 'Berlaku', klasifikasi = ? WHERE id = ?", (req.klasifikasi, doc_id))
    conn.commit()
    conn.close()
    
    # Trigger vector DB injection and KG asynchronously
    filename = os.path.basename(local_path)
    background_tasks.add_task(ingest_document_background, local_path, doc_id, filename, nomor, jenis, sektor, 'Berlaku', req.klasifikasi)
    
    return {"message": "Dokumen berhasil dikonfirmasi dan dimasukkan ke repositori."}

"""
if "ConfirmPendingRequest" not in content:
    content = content.replace(class_def, confirm_endpoint_and_class + class_def)

# 4. Modify process_document_background to use LLM and stop
#    and create ingest_document_background

old_process = """def process_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    try:
        parser = LegalDocumentParser()
        chunker = LegalChunker()
        
        full_text = parser.parse_pdf(file_path)
        
        # ── Duplicate Detection (FR-5) ──────────────────────────────────────"""
        
new_process = """def process_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    try:
        parser = LegalDocumentParser()
        full_text = parser.parse_pdf(file_path)
        
        # ── AI Recommendation (FR-4) ──────────────────────────────────────
        messages = [
            {"role": "system", "content": "Anda adalah analis regulasi korporat. Tugas Anda adalah memberikan rekomendasi tingkat kerahasiaan dokumen berdasarkan isinya. Balas hanya dengan satu kata: 'Umum', 'Rahasia', atau 'Terbatas'."},
            {"role": "user", "content": f"Teks dokumen:\\n{full_text[:3000]}\\n\\nBerdasarkan teks ini, rekomendasikan klasifikasi: Umum, Rahasia, atau Terbatas."}
        ]
        
        recommended_klasifikasi = "Umum"
        try:
            from services.llm import call_glm
            raw_content = call_glm(messages, temperature=0.1, timeout=30)
            raw_content = raw_content.lower()
            if "terbatas" in raw_content:
                recommended_klasifikasi = "Terbatas"
            elif "rahasia" in raw_content:
                recommended_klasifikasi = "Rahasia"
        except Exception as llm_err:
            print(f"LLM classification error: {llm_err}")
            
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
            conn = sqlite3.connect(os.path.join(BASE_DIR, "data", "legal_metadata.db"), timeout=30.0)
            c = conn.cursor()
            c.execute("UPDATE regulations SET status = 'Gagal - Error' WHERE id = ?", (doc_id,))
            conn.commit()
            conn.close()
        except:
            pass

def ingest_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    try:
        parser = LegalDocumentParser()
        chunker = LegalChunker()
        
        full_text = parser.parse_pdf(file_path)
        
        # ── Duplicate Detection (FR-5) ──────────────────────────────────────"""

content = content.replace(old_process, new_process)


with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("Patch applied to main.py successfully.")
