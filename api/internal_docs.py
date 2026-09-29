from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from auth import get_current_user
import uuid
import os
import shutil
import sys

from services import pg_service
from services.embed_service import embed_documents

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "parser"))
from pdf_parser import LegalChunker
from services.rag_service import extract_text_hybrid

PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")

router = APIRouter()

# Migration M3 (cutover): internal-document uploads moved from the ChromaDB
# 'ojk_regulations' collection to the unified PG chunks table. Metadata keys
# map 1:1 to columns; doc_id stays NULL (legacy rows carried no reg_id).
# These chunks were never part of the legacy FTS index -> sparse_legacy=FALSE.

@router.post("/upload-internal")
async def upload_internal_doc(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    upload_id = str(uuid.uuid4())[:8]
    temp_path = os.path.join(PDFS_DIR, f"internal_{upload_id}_{file.filename}")
    
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        # (R1 bugfix: hybrid PyMuPDF+VLM extraction replaces the dead
        # PaddleOCR chain; see la-legpro-doc/bug_reports.md.)
        full_text = extract_text_hybrid(temp_path)
        
        # (Bugfix M3: legacy called LegalChunker(chunk_size=1000, overlap=200)
        # but the class takes no constructor args -- the endpoint 500'd on
        # every call. See la-legpro-doc/bug_reports.md.)
        chunker = LegalChunker()
        chunks = chunker.chunk_document(full_text)
        
        if not chunks:
            raise HTTPException(status_code=400, detail="Could not extract text from the document.")
            
        # chunk_document returns {"text", "metadata"} dicts -- embed the indexed
        # text and carry window_context, mirroring the repository ingest path.
        ids = [f"internal_{upload_id}_{i}" for i in range(len(chunks))]
        texts = [c["text"] for c in chunks]
        windows = [(c.get("metadata") or {}).get("window_context") for c in chunks]
        embeddings = embed_documents(texts)

        with pg_service.get_conn() as conn:
            for chunk_id, text, window, emb in zip(ids, texts, windows, embeddings):
                conn.execute(
                    "INSERT INTO chunks (id, doc_id, text, window_context, jenis, nomor, "
                    "sektor, judul, visibility, user_id, embedding, sparse_legacy) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, FALSE) "
                    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                    "window_context = EXCLUDED.window_context, "
                    "embedding = EXCLUDED.embedding",
                    (chunk_id, None, text, window, "Dokumen Internal", file.filename,
                     "Internal", file.filename, "private", str(current_user["id"]), emb))
        
        return {"status": "success", "message": f"Successfully ingested {len(chunks)} chunks.", "filename": file.filename}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error ingesting internal doc: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
