from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from typing import Optional
import json
import logging
import os
import shutil
import uuid

from auth import get_current_user
from services import pg_service
from services.embed_service import embed_documents

# FEAT phase-2b (2026-10-24): source-file persistence for the pengguna
# notebook preview. /check-compliance keeps only a temp file (deleted in its
# `finally`), so the analyzed document's bytes never survived; the frontend
# now attaches the original file right after the report row is saved and the
# notebook detail page previews it. Storage is id-derived (report_id + the
# extension of the stored filename) so no schema change is needed. NOTE:
# files live in the backend container's data dir (same data/pdfs root); a
# container recreate without a volume/backup drops them — the preview then
# falls back to the summary card.

# Migration M3 (cutover): chat/compliance persistence moved from users.db to
# PostgreSQL (services.pg_service), and the compliance-report auto-ingest
# moved from the ChromaDB 'ojk_regulations' collection to the unified PG
# chunks table (embedded with services.embed_service, the same MiniLM model
# Chroma's default embedding function used).

router = APIRouter()

class ChatSessionCreate(BaseModel):
    title: str

class ChatSessionUpdate(BaseModel):
    messages: list

@router.post("/chat-sessions")
def create_chat_session(req: ChatSessionCreate, current_user: dict = Depends(get_current_user)):
    session_id = str(uuid.uuid4())
    pg_service.execute(
        "INSERT INTO chat_sessions (id, user_id, title) VALUES (%s, %s, %s)",
        (session_id, current_user["id"], req.title))
    return {"session_id": session_id}

@router.get("/chat-sessions")
def get_chat_sessions(current_user: dict = Depends(get_current_user)):
    sessions = pg_service.query(
        "SELECT id, title, to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM chat_sessions WHERE user_id = %s ORDER BY created_at DESC",
        (current_user["id"],))
    return {"sessions": sessions}

@router.get("/chat-sessions/{session_id}/messages")
def get_chat_messages(session_id: str, current_user: dict = Depends(get_current_user)):
    # Verify ownership
    if not pg_service.query_one(
            "SELECT id FROM chat_sessions WHERE id = %s AND user_id = %s",
            (session_id, current_user["id"])):
        raise HTTPException(status_code=403, detail="Not authorized or session not found")

    rows = pg_service.query(
        "SELECT role, content, sources_json FROM chat_messages "
        "WHERE session_id = %s ORDER BY id ASC",
        (session_id,))

    messages = []
    for row in rows:
        msg = {
            "role": row["role"],
            "content": row["content"]
        }
        if row["sources_json"]:
            msg["sources"] = json.loads(row["sources_json"])
        messages.append(msg)
    return {"messages": messages}

@router.post("/chat-sessions/{session_id}/messages")
def add_chat_messages(session_id: str, req: ChatSessionUpdate, current_user: dict = Depends(get_current_user)):
    # Verify ownership
    if not pg_service.query_one(
            "SELECT id FROM chat_sessions WHERE id = %s AND user_id = %s",
            (session_id, current_user["id"])):
        raise HTTPException(status_code=403, detail="Not authorized or session not found")

    # Single transaction: all messages + the session touch commit together.
    with pg_service.get_conn() as conn:
        for msg in req.messages:
            sources_json = json.dumps(msg.get("sources", [])) if "sources" in msg else None
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, sources_json) "
                "VALUES (%s, %s, %s, %s)",
                (session_id, msg["role"], msg["content"], sources_json))

        conn.execute(
            "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (session_id,))
    return {"status": "ok"}

class ComplianceReportSave(BaseModel):
    filename: str
    company_name: Optional[str] = None
    expiration_date: Optional[str] = None
    results: dict
    # FEAT grouped notebooks (2026-10-24, user: "weren't it supposed to
    # appear in the workspace I uploaded it into?"): when the upload modal
    # is opened from inside a notebook, the frontend passes that notebook's
    # id and the new document joins its document list. Absent -> the report
    # becomes the first document of its own notebook (notebook_id = id).
    notebook_id: Optional[str] = None

# Notebook identity follows migration 014: notebook_id == the id of the
# first (oldest) document of the group, so /contracts/{id} deep links keep
# working for legacy rows.

class ComplianceReportUpdate(BaseModel):
    company_name: Optional[str] = None
    expiration_date: Optional[str] = None

@router.post("/compliance-history")
def save_compliance_report(req: ComplianceReportSave, current_user: dict = Depends(get_current_user)):
    report_id = str(uuid.uuid4())
    results_json = json.dumps(req.results)

    # Notebook membership: an upload started inside a notebook passes its
    # notebook id (owner-checked); anything else roots its own notebook.
    # Migration 017: an empty workspace mints its own row in `notebooks` —
    # the first document joins it under that SAME id, which stays the
    # workspace's identity afterwards.
    if req.notebook_id:
        if not pg_service.query_one(
                "SELECT id FROM compliance_history WHERE id = %s AND user_id = %s",
                (req.notebook_id, current_user["id"])) \
        and not pg_service.query_one(
                "SELECT id FROM notebooks WHERE id = %s AND user_id = %s",
                (req.notebook_id, current_user["id"])):
            raise HTTPException(status_code=404, detail="Notebook not found")
        notebook_id = req.notebook_id
    else:
        notebook_id = report_id

    # expiration_date: '' (legacy empty string) becomes NULL -- the PG DATE
    # column cannot store empty strings.
    pg_service.execute(
        "INSERT INTO compliance_history (id, user_id, filename, company_name, expiration_date, results_json, notebook_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (report_id, current_user["id"], req.filename, req.company_name,
         req.expiration_date or None, results_json, notebook_id))

    # Automatically ingest into Knowledge Base
    # (Migration M3: was Chroma collection.add on 'ojk_regulations'; now the
    # unified PG chunks table. sparse_legacy stays FALSE -- these clauses
    # were never part of the legacy FTS/BM25 index.)
    try:
        docs = []
        metadatas = []
        ids = []

        summary_data = req.results.get("summary", {})
        clauses = req.results.get("results", [])

        for i, clause in enumerate(clauses):
            pasal = clause.get('pasal', 'Pasal')
            isi = clause.get('isi_pasal', '')
            if not isi: continue

            clause_text = f"[{pasal}] {isi}"
            docs.append(clause_text)
            metadatas.append({
                "doc_id": report_id,
                "domain": "analyzed_document",
                "judul": req.filename,
                "nomor": pasal,
                "jenis": summary_data.get("jenis_dokumen", "Kontrak"),
                "sektor": summary_data.get("sektor_bisnis", "umum"),
                "status": "user_uploaded",
                "filename": req.filename,
                "visibility": "private",
                "user_id": current_user["id"]
            })
            ids.append(f"{report_id}_c{i}")

        if docs:
            embeddings = embed_documents(docs)
            with pg_service.get_conn() as conn:
                for chunk_id, text, meta, emb in zip(ids, docs, metadatas, embeddings):
                    conn.execute(
                        "INSERT INTO chunks (id, doc_id, text, domain, jenis, judul, nomor, "
                        "sektor, status, filename, visibility, user_id, embedding, sparse_legacy) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, FALSE) "
                        "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                        "embedding = EXCLUDED.embedding",
                        (chunk_id, meta["doc_id"], text, meta["domain"], meta["jenis"],
                         meta["judul"], meta["nomor"], meta["sektor"], meta["status"],
                         meta["filename"], meta["visibility"], meta["user_id"], emb))
            print(f"[History] Ingested {len(docs)} clauses into PG chunks for report {report_id}")
    except Exception as e:
        print(f"[History] Failed to ingest into chunk store: {e}")

    return {"report_id": report_id, "notebook_id": notebook_id}

@router.get("/compliance-history")
def get_compliance_history(current_user: dict = Depends(get_current_user)):
    reports = pg_service.query(
        "SELECT id, filename, company_name, "
        "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at, notebook_id "
        "FROM compliance_history WHERE user_id = %s ORDER BY created_at DESC",
        (current_user["id"],))
    return {"history": reports}

class NotebookCreate(BaseModel):
    # Migration 017: optional name for the new workspace; the FE supplies a
    # locale-appropriate default ("Untitled workspace").
    name: Optional[str] = None

@router.post("/compliance-history/notebooks")
def create_empty_notebook(req: NotebookCreate, current_user: dict = Depends(get_current_user)):
    """Mint an EMPTY workspace (migration 017): a workspace the user can open
    and upload into before any document exists. The id lives only in the
    notebooks table (never reused as a document id), so the legacy
    notebook_id-equals-root-document identity is preserved for every
    document-rooted group. Once the first document joins, the id stays the
    workspace's identity: uploads pass it as notebook_id and the home card
    keeps counting through it."""
    notebook_id = str(uuid.uuid4())
    name = (req.name or "").strip() or "Untitled workspace"
    pg_service.execute(
        "INSERT INTO notebooks (id, user_id, name) VALUES (%s, %s, %s)",
        (notebook_id, current_user["id"], name))
    return {"notebook_id": notebook_id, "name": name}

@router.get("/compliance-history/notebooks")
def get_compliance_notebooks(current_user: dict = Depends(get_current_user)):
    """Notebook-level list for the pengguna home cards: one row per notebook
    (a row whose notebook_id equals its own id) plus its document count.
    Migration 017: workspaces created empty carry their own row in
    `notebooks`; since that id never becomes a document id, the derived
    query above can never emit it twice — union both namespaces with a
    plain merge (both queries already sort created_at DESC)."""
    reports = pg_service.query(
        "SELECT id, filename, company_name, "
        "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at, "
        "(SELECT count(*) FROM compliance_history d "
        " WHERE d.notebook_id = h.notebook_id) AS document_count, "
        "(SELECT to_char(max(d.created_at), 'YYYY-MM-DD HH24:MI:SS') "
        " FROM compliance_history d WHERE d.notebook_id = h.notebook_id) AS modified_at "
        "FROM compliance_history h "
        "WHERE h.user_id = %s AND h.notebook_id = h.id ORDER BY h.created_at DESC",
        (current_user["id"],))
    emptied = pg_service.query(
        "SELECT n.id, '' AS filename, n.name AS company_name, "
        "NULL::text AS expiration_date, "
        "to_char(n.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at, "
        "(SELECT count(*) FROM compliance_history d "
        " WHERE d.notebook_id = n.id) AS document_count, "
        "(SELECT to_char(max(d.created_at), 'YYYY-MM-DD HH24:MI:SS') "
        " FROM compliance_history d WHERE d.notebook_id = n.id) AS modified_at "
        "FROM notebooks n WHERE n.user_id = %s",
        (current_user["id"],))
    return {"notebooks": sorted(list(reports) + list(emptied),
                                key=lambda r: r["created_at"], reverse=True)}

@router.get("/compliance-history/{report_id}/documents")
def get_notebook_documents(report_id: str, current_user: dict = Depends(get_current_user)):
    """All documents of the notebook the given report belongs to (the
    notebook page's middle column list). Oldest first: the notebook's first
    document is its identity / deep-link id.
    Migration 017: report_id may also be an EMPTY workspace's notebooks-row
    id (minted at creation, exact identity even after documents join). Those
    return an (possibly empty) list plus the stored notebook_name for the
    page's header/crumb."""
    own = pg_service.query_one(
        "SELECT notebook_id FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))
    notebook_name = None
    if own and own["notebook_id"]:
        notebook_id = own["notebook_id"]
    else:
        nb = pg_service.query_one(
            "SELECT name FROM notebooks WHERE id = %s AND user_id = %s",
            (report_id, current_user["id"]))
        if not nb:
            raise HTTPException(status_code=404, detail="Report not found")
        notebook_id = report_id
        notebook_name = nb["name"]

    docs = pg_service.query(
        "SELECT id, filename, company_name, "
        "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM compliance_history "
        "WHERE user_id = %s AND notebook_id = %s ORDER BY created_at ASC",
        (current_user["id"], notebook_id))
    result: dict = {"documents": docs}
    if notebook_name:
        result["notebook_name"] = notebook_name
    return result

@router.get("/compliance-history/{report_id}")
def get_compliance_report_detail(report_id: str, current_user: dict = Depends(get_current_user)):
    row = pg_service.query_one(
        "SELECT id, filename, company_name, "
        "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, results_json, "
        "notebook_id, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))

    if not row:
        raise HTTPException(status_code=404, detail="Report not found")

    # The workspace trail (Beranda > notebook > document) needs the owning
    # notebook's display name: the notebook row's company, falling back to
    # its file name (same rule the notebook page uses). For a ROOT document
    # the notebook row IS the row itself — its name still belongs in the
    # trail (user example: Home > QA Pratinjau Berkas > qa-uji-pratinjau.pdf).
    notebook_name = None
    if row["notebook_id"]:
        nb = pg_service.query_one(
            "SELECT company_name, filename FROM compliance_history WHERE id = %s",
            (row["notebook_id"],))
        if nb:
            notebook_name = nb["company_name"] or nb["filename"]
        else:
            # Migration 017: an empty-minted workspace's id lives only in the
            # notebooks table (never as a document id) — the name comes from
            # there.
            ws = pg_service.query_one(
                "SELECT name FROM notebooks WHERE id = %s", (row["notebook_id"],))
            if ws:
                notebook_name = ws["name"]

    return {
        "id": row["id"],
        "filename": row["filename"],
        "company_name": row["company_name"],
        "expiration_date": row["expiration_date"],
        "created_at": row["created_at"],
        # The workspace's back link goes to the owning notebook page.
        "notebook_id": row["notebook_id"],
        # Notebook display name for the topbar path trail (phase-3).
        "notebook_name": notebook_name,
        "results": json.loads(row["results_json"])
    }

def _contract_file_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "contracts")

def _contract_file_path(report_id: str, stored_filename: str) -> str:
    """The stored path is id-derived: {report_id}{ext of the history row's
    filename}. The extension never changes after upload, so serving needs
    no extra column."""
    ext = os.path.splitext(stored_filename)[1].lower()
    return os.path.join(_contract_file_dir(), f"{report_id}{ext}")

@router.post("/compliance-history/{report_id}/file")
async def save_compliance_report_file(
    report_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Persist the analyzed source file so the notebook preview can render
    it. Called by the frontend right after the report row is saved (the
    analyze endpoint deleted its temp copy). Failure is non-fatal for the
    upload flow — the preview falls back to the summary card."""
    row = pg_service.query_one(
        "SELECT id, filename FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")

    ext = os.path.splitext(row["filename"])[1].lower()
    if not ext:
        raise HTTPException(status_code=400, detail="Filename has no extension; source file cannot be stored.")

    MAX_FILE_SIZE = 50 * 1024 * 1024  # same cap as /check-compliance
    target = _contract_file_path(report_id, row["filename"])
    os.makedirs(os.path.dirname(target), exist_ok=True)
    written = 0
    try:
        with open(target, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="File too large (max 50MB)")
                buffer.write(chunk)
    finally:
        await file.close()

    return {"status": "saved", "bytes": written}

@router.get("/compliance-history/{report_id}/file")
def get_compliance_report_file(report_id: str, current_user: dict = Depends(get_current_user)):
    """Serve the stored source file as base64 JSON (the same pattern as
    /api/pdf/{filename}: JSON responses keep the corporate IDM from
    intercepting, and the frontend decodes + renders it as a blob URL)."""
    import base64
    row = pg_service.query_one(
        "SELECT id, filename FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")

    target = _contract_file_path(report_id, row["filename"])
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="Source file not stored for this report")

    with open(target, "rb") as f:
        return {
            "filename": row["filename"],
            "ext": os.path.splitext(row["filename"])[1].lower().lstrip("."),
            "data": base64.b64encode(f.read()).decode("utf-8"),
        }

@router.put("/compliance-history/{report_id}")
def update_compliance_report(report_id: str, req: ComplianceReportUpdate, current_user: dict = Depends(get_current_user)):
    pg_service.execute(
        "UPDATE compliance_history SET company_name = %s, expiration_date = %s "
        "WHERE id = %s AND user_id = %s",
        (req.company_name, req.expiration_date or None, report_id, current_user["id"]))
    return {"status": "ok"}

@router.delete("/compliance-history/{report_id}")
def delete_compliance_report(report_id: str, current_user: dict = Depends(get_current_user)):
    row = pg_service.query_one(
        "SELECT id, filename, notebook_id FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))
    if not row:
        # Migration 017: the id may belong to an EMPTY workspace's notebooks
        # row. Deleting it removes the workspace and every document joined to
        # it (with their stored source files), then the workspace itself.
        nb = pg_service.query_one(
            "SELECT id, name FROM notebooks WHERE id = %s AND user_id = %s",
            (report_id, current_user["id"]))
        if not nb:
            raise HTTPException(status_code=404, detail="Report not found")
        joined = pg_service.query(
            "SELECT id, filename FROM compliance_history "
            "WHERE notebook_id = %s AND user_id = %s",
            (nb["id"], current_user["id"]))
        for doc in joined:
            try:
                target = _contract_file_path(doc["id"], doc["filename"])
                if os.path.exists(target):
                    os.remove(target)
            except OSError:
                logging.getLogger(__name__).exception("removing stored source file failed (report %s)", doc["id"])
        pg_service.execute(
            "DELETE FROM compliance_history WHERE notebook_id = %s AND user_id = %s",
            (nb["id"], current_user["id"]))
        pg_service.execute(
            "DELETE FROM notebooks WHERE id = %s AND user_id = %s",
            (nb["id"], current_user["id"]))
        return {"status": "deleted"}

    # Notebook cascade (migration 014 semantics): deleting the notebook's
    # root document deletes the whole notebook, bundled files included.
    # A plain child document only removes itself.
    doomed: list = [(row["id"], row["filename"])]
    if row["notebook_id"] and row["notebook_id"] == row["id"]:
        children = pg_service.query(
            "SELECT id, filename FROM compliance_history "
            "WHERE notebook_id = %s AND user_id = %s AND id <> %s",
            (row["id"], current_user["id"], row["id"]))
        doomed.extend((c["id"], c["filename"]) for c in children)

    # NOTE: same as the legacy behaviour, ingested clause chunks are NOT
    # removed when a report is deleted. Stored source files (phase-2b
    # preview persistence) ARE removed — a dangling file has no owner.
    is_root = bool(row["notebook_id"]) and row["notebook_id"] == row["id"]
    for doc_id, filename in doomed:
        try:
            target = _contract_file_path(doc_id, filename)
            if os.path.exists(target):
                os.remove(target)
        except OSError:
            logging.getLogger(__name__).exception("removing stored source file failed (report %s)", doc_id)
    if is_root:
        pg_service.execute(
            "DELETE FROM compliance_history WHERE notebook_id = %s AND user_id = %s",
            (row["id"], current_user["id"]))
    else:
        pg_service.execute(
            "DELETE FROM compliance_history WHERE id = %s AND user_id = %s",
            (report_id, current_user["id"]))
    return {"status": "deleted"}

@router.post("/trigger-compliance-alerts")
def test_alerts(current_user: dict = Depends(get_current_user)):
    from services.alert_scheduler import check_expiring_contracts
    try:
        check_expiring_contracts()
        return {"message": "Alerts check completed successfully. Check logs/email."}
    except Exception:
        logging.getLogger(__name__).exception("test_alerts failed")
        raise HTTPException(status_code=500, detail="Internal server error")

@router.get("/compliance-history/{report_id}/calendar")
def export_calendar(report_id: str, current_user: dict = Depends(get_current_user)):
    from fastapi.responses import Response
    import datetime

    row = pg_service.query_one(
        "SELECT company_name, expiration_date FROM compliance_history "
        "WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))

    if not row or not row["expiration_date"]:
        raise HTTPException(status_code=404, detail="Expiration date not found for this report.")

    company_name = row["company_name"] or "Contract"
    # expiration_date is a datetime.date now (PG DATE column) -- the legacy
    # YYYY-MM-DD string parsing (and its ValueError -> 400 branch) is gone.
    exp_date = row["expiration_date"]
    expiry_date_str = exp_date.isoformat()

    dt = datetime.datetime.combine(exp_date, datetime.time.min)
    dt_start = dt.strftime("%Y%m%d")
    dt_end = (dt + datetime.timedelta(days=1)).strftime("%Y%m%d")
    now_str = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')

    ics_content = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//LegalAnalyzer//ContractMonitor//EN\n"
    ics_content += f"BEGIN:VEVENT\nUID:{report_id}@legal-analyzer.com\nDTSTAMP:{now_str}\n"
    ics_content += f"DTSTART;VALUE=DATE:{dt_start}\nDTEND;VALUE=DATE:{dt_end}\n"
    ics_content += f"SUMMARY:Contract Expiry: {company_name}\n"
    ics_content += f"DESCRIPTION:Contract with {company_name} is expiring on {expiry_date_str}.\n"
    ics_content += "END:VEVENT\nEND:VCALENDAR"

    return Response(content=ics_content, media_type="text/calendar", headers={
        "Content-Disposition": f"attachment; filename=contract_expiry_{company_name.replace(' ', '_')}.ics"
    })
