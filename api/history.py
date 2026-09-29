from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import json
import uuid

from auth import get_current_user
from services import pg_service
from services.embed_service import embed_documents

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

class ComplianceReportUpdate(BaseModel):
    company_name: Optional[str] = None
    expiration_date: Optional[str] = None

@router.post("/compliance-history")
def save_compliance_report(req: ComplianceReportSave, current_user: dict = Depends(get_current_user)):
    report_id = str(uuid.uuid4())
    results_json = json.dumps(req.results)

    # expiration_date: '' (legacy empty string) becomes NULL -- the PG DATE
    # column cannot store empty strings.
    pg_service.execute(
        "INSERT INTO compliance_history (id, user_id, filename, company_name, expiration_date, results_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (report_id, current_user["id"], req.filename, req.company_name,
         req.expiration_date or None, results_json))

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

    return {"report_id": report_id}

@router.get("/compliance-history")
def get_compliance_history(current_user: dict = Depends(get_current_user)):
    reports = pg_service.query(
        "SELECT id, filename, company_name, "
        "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM compliance_history WHERE user_id = %s ORDER BY created_at DESC",
        (current_user["id"],))
    return {"history": reports}

@router.get("/compliance-history/{report_id}")
def get_compliance_report_detail(report_id: str, current_user: dict = Depends(get_current_user)):
    row = pg_service.query_one(
        "SELECT id, filename, company_name, "
        "to_char(expiration_date, 'YYYY-MM-DD') AS expiration_date, results_json, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM compliance_history WHERE id = %s AND user_id = %s",
        (report_id, current_user["id"]))

    if not row:
        raise HTTPException(status_code=404, detail="Report not found")

    return {
        "id": row["id"],
        "filename": row["filename"],
        "company_name": row["company_name"],
        "expiration_date": row["expiration_date"],
        "created_at": row["created_at"],
        "results": json.loads(row["results_json"])
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
    # NOTE: same as the legacy behaviour, ingested clause chunks are NOT
    # removed when a report is deleted.
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
