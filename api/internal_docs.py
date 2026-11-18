from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from auth import get_current_user
import re
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

# B3: a workspace id is a compliance_history report id (UUID string). Validate at
# the trust boundary: optional, but if present it must look like a report id. We
# do NOT verify it exists/belongs to the user — report_id only NARROWS this
# user's own private chunks (subtractive), so a bogus value can only fail to
# match, never widen access. Cheap and decoupled, like rag_sources._valid_source_ref.
_REPORT_ID_RE = re.compile(r"^[0-9a-fA-F-]{1,64}$")   # UUID-ish; bounded length

# Migration M3 (cutover): internal-document uploads moved from the ChromaDB
# 'ojk_regulations' collection to the unified PG chunks table. Metadata keys
# map 1:1 to columns; doc_id stays NULL (legacy rows carried no reg_id).
# These chunks were never part of the legacy FTS index -> sparse_legacy=FALSE.

_ALLOWED_REF_EXTS = {".pdf", ".docx", ".pptx"}


def _extract_docx_text(file_path: str) -> str:
    import docx

    doc = docx.Document(file_path)
    parts: list[str] = []
    for p in doc.paragraphs:
        txt = (p.text or "").strip()
        if txt:
            parts.append(txt)
    for table in doc.tables:
        for row in table.rows:
            row_cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if row_cells:
                parts.append(" | ".join(row_cells))
    return "\n".join(parts)


def _extract_pptx_text(file_path: str) -> str:
    from pptx import Presentation

    prs = Presentation(file_path)
    slides_text: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        slide_parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                txt = "\n".join(
                    p.text.strip() for p in shape.text_frame.paragraphs if p.text and p.text.strip()
                )
                if txt:
                    slide_parts.append(txt)
            if shape.has_table:
                for row in shape.table.rows:
                    row_cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if row_cells:
                        slide_parts.append(" | ".join(row_cells))
        if slide_parts:
            slides_text.append(f"[Slide {idx}]\n" + "\n".join(slide_parts))
    return "\n\n".join(slides_text)


def _extract_reference_text(file_path: str, ext: str) -> str:
    if ext == ".pdf":
        return extract_text_hybrid(file_path)
    if ext == ".docx":
        return _extract_docx_text(file_path)
    if ext == ".pptx":
        return _extract_pptx_text(file_path)
    raise HTTPException(status_code=400, detail="Only PDF, DOCX, and PPTX files are supported.")


_UPLOAD_ID_RE = re.compile(r"^[0-9a-fA-F]{8}$")


class CheckInternalDuplicatesRequest(BaseModel):
    filenames: list[str]
    report_id: str | None = None


@router.post("/upload-internal/check-duplicates")
def check_internal_duplicates(
    req: CheckInternalDuplicatesRequest,
    current_user: dict = Depends(get_current_user),
):
    if req.report_id is not None and not _REPORT_ID_RE.match(req.report_id):
        raise HTTPException(status_code=400, detail="Invalid report_id.")
    names = [os.path.basename(n) for n in req.filenames if n]
    if not names:
        return {"duplicates": []}

    uid = str(current_user["id"])
    rid = req.report_id if req.report_id else None
    rows = pg_service.query(
        "SELECT split_part(id, '_', 2) AS upload_id, min(judul) AS filename "
        "FROM chunks "
        "WHERE visibility = 'private' AND user_id = %s::text "
        "AND doc_id IS NULL AND id LIKE 'internal\\_%%' "
        "AND (report_id IS NULL OR report_id = %s) "
        "AND judul = ANY(%s) "
        "GROUP BY split_part(id, '_', 2)",
        (uid, rid, names),
    )
    return {
        "duplicates": [
            {"upload_id": r["upload_id"], "filename": r["filename"]}
            for r in rows
        ]
    }


@router.post("/upload-internal")
def upload_internal_doc(
    file: UploadFile = File(...),
    report_id: str | None = Form(None),
    replace: bool = Form(False),
    current_user: dict = Depends(get_current_user),
):
    safe_name = os.path.basename(file.filename or "")
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in _ALLOWED_REF_EXTS:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, and PPTX files are supported.")
    if report_id is not None and not _REPORT_ID_RE.match(report_id):
        raise HTTPException(status_code=400, detail="Invalid report_id.")

    upload_id = str(uuid.uuid4())[:8]
    temp_path = os.path.join(PDFS_DIR, f"internal_{upload_id}_{safe_name}")
    
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        full_text = _extract_reference_text(temp_path, ext)
        
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
            if replace:
                # Replace any existing private internal chunks with the same filename in this scope
                old_rows = conn.execute(
                    "SELECT DISTINCT split_part(id, '_', 2) AS upload_id "
                    "FROM chunks "
                    "WHERE visibility = 'private' AND user_id = %s::text "
                    "AND doc_id IS NULL AND id LIKE 'internal\\_%%' "
                    "AND (report_id IS NULL OR report_id = %s) "
                    "AND judul = %s",
                    (str(current_user["id"]), report_id if report_id else None, safe_name),
                ).fetchall()
                for old in old_rows:
                    old_uid = old["upload_id"]
                    conn.execute(
                        "DELETE FROM chunks WHERE visibility = 'private' AND user_id = %s::text "
                        "AND doc_id IS NULL AND id LIKE %s",
                        (str(current_user["id"]), f"internal_{old_uid}_%"),
                    )
            for chunk_id, text, window, emb in zip(ids, texts, windows, embeddings):
                conn.execute(
                    "INSERT INTO chunks (id, doc_id, text, window_context, jenis, nomor, "
                    "sektor, judul, visibility, user_id, report_id, embedding, sparse_legacy) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, FALSE) "
                    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                    "window_context = EXCLUDED.window_context, "
                    "embedding = EXCLUDED.embedding, report_id = EXCLUDED.report_id",
                    (chunk_id, None, text, window, "Dokumen Internal", file.filename,
                     "Internal", file.filename, "private", str(current_user["id"]),
                     report_id, emb))
        
        return {"status": "success", "message": f"Successfully ingested {len(chunks)} chunks.", "filename": file.filename}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error ingesting internal doc: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.delete("/upload-internal/{upload_id}")
def delete_internal_doc(
    upload_id: str,
    current_user: dict = Depends(get_current_user),
):
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload_id.")

    uid = str(current_user["id"])
    like_pattern = f"internal_{upload_id.lower()}_%"
    source_ref = f"internal:{upload_id.lower()}"

    with pg_service.get_conn() as conn:
        with conn.transaction():
            cur = conn.execute(
                "DELETE FROM chunks "
                "WHERE visibility = 'private' AND user_id = %s::text "
                "AND doc_id IS NULL AND id LIKE %s",
                (uid, like_pattern),
            )
            deleted_chunks = cur.rowcount or 0
            conn.execute(
                "DELETE FROM rag_source_exclusions "
                "WHERE user_id = %s AND source_ref = %s",
                (uid, source_ref),
            )

    if deleted_chunks == 0:
        raise HTTPException(status_code=404, detail="Internal reference not found.")

    return {"status": "deleted", "upload_id": upload_id.lower(), "deleted_chunks": deleted_chunks}
