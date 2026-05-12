from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from auth import get_current_user
import chromadb
import uuid
import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "parser"))
from pdf_parser import LegalDocumentParser, LegalChunker

CHROMA_DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")

router = APIRouter()

def get_chroma_collection():
    client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    return client.get_or_create_collection(
        name="ojk_regulations",
        metadata={"hnsw:space": "cosine"}
    )

@router.post("/upload-internal")
async def upload_internal_doc(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")
        
    upload_id = str(uuid.uuid4())[:8]
    temp_path = os.path.join(PDFS_DIR, f"internal_{upload_id}_{file.filename}")
    
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        parser = LegalDocumentParser()
        full_text = parser.parse_pdf(temp_path)
        
        chunker = LegalChunker(chunk_size=1000, overlap=200)
        chunks = chunker.chunk_document(full_text)
        
        if not chunks:
            raise HTTPException(status_code=400, detail="Could not extract text from the document.")
            
        collection = get_chroma_collection()
        
        ids = []
        metadatas = []
        documents = []
        
        for i, chunk in enumerate(chunks):
            chunk_id = f"internal_{upload_id}_{i}"
            ids.append(chunk_id)
            documents.append(chunk)
            metadatas.append({
                "jenis": "Dokumen Internal",
                "nomor": file.filename,
                "sektor": "Internal",
                "judul": file.filename,
                "visibility": "private",
                "user_id": current_user["id"]
            })
            
        collection.add(
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )
        
        return {"status": "success", "message": f"Successfully ingested {len(chunks)} chunks.", "filename": file.filename}
        
    except Exception as e:
        print(f"Error ingesting internal doc: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
