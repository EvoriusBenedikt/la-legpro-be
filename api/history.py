from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import sqlite3
import json
import uuid
import os
import shutil
import time
import chromadb

from auth import get_current_user, get_db_connection

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

_chroma_client = None
def get_chroma_collection():
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    return _chroma_client.get_or_create_collection(
        name="ojk_regulations",
        metadata={"hnsw:space": "cosine"}
    )

router = APIRouter()

class ChatSessionCreate(BaseModel):
    title: str

class ChatSessionUpdate(BaseModel):
    messages: list

@router.post("/chat-sessions")
def create_chat_session(req: ChatSessionCreate, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    session_id = str(uuid.uuid4())
    c.execute("INSERT INTO chat_sessions (id, user_id, title) VALUES (?, ?, ?)", 
              (session_id, current_user["id"], req.title))
    conn.commit()
    conn.close()
    return {"session_id": session_id}

@router.get("/chat-sessions")
def get_chat_sessions(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, title, created_at FROM chat_sessions WHERE user_id = ? ORDER BY created_at DESC", 
              (current_user["id"],))
    sessions = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"sessions": sessions}

@router.get("/chat-sessions/{session_id}/messages")
def get_chat_messages(session_id: str, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    # Verify ownership
    c.execute("SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?", (session_id, current_user["id"]))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Not authorized or session not found")
        
    c.execute("SELECT role, content, sources_json FROM chat_messages WHERE session_id = ? ORDER BY id ASC", 
              (session_id,))
    
    messages = []
    for row in c.fetchall():
        msg = {
            "role": row["role"],
            "content": row["content"]
        }
        if row["sources_json"]:
            msg["sources"] = json.loads(row["sources_json"])
        messages.append(msg)
    conn.close()
    return {"messages": messages}

@router.post("/chat-sessions/{session_id}/messages")
def add_chat_messages(session_id: str, req: ChatSessionUpdate, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    # Verify ownership
    c.execute("SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?", (session_id, current_user["id"]))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Not authorized or session not found")
        
    for msg in req.messages:
        sources_json = json.dumps(msg.get("sources", [])) if "sources" in msg else None
        c.execute("INSERT INTO chat_messages (session_id, role, content, sources_json) VALUES (?, ?, ?, ?)",
                  (session_id, msg["role"], msg["content"], sources_json))
        
    c.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
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
    conn = get_db_connection()
    c = conn.cursor()
    report_id = str(uuid.uuid4())
    results_json = json.dumps(req.results)
    
    c.execute("INSERT INTO compliance_history (id, user_id, filename, company_name, expiration_date, results_json) VALUES (?, ?, ?, ?, ?, ?)",
              (report_id, current_user["id"], req.filename, req.company_name, req.expiration_date, results_json))
    conn.commit()
    conn.close()
    
    # Automatically ingest into Knowledge Base
    try:
        collection = get_chroma_collection()
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
                "reg_id": report_id,
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
            collection.add(
                documents=docs,
                metadatas=metadatas,
                ids=ids
            )
            print(f"[History] Ingested {len(docs)} clauses into ChromaDB for report {report_id}")
    except Exception as e:
        print(f"[History] Failed to ingest into ChromaDB: {e}")

    return {"report_id": report_id}

@router.get("/compliance-history")
def get_compliance_history(current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, filename, company_name, expiration_date, created_at FROM compliance_history WHERE user_id = ? ORDER BY created_at DESC", 
              (current_user["id"],))
    reports = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"history": reports}

@router.get("/compliance-history/{report_id}")
def get_compliance_report_detail(report_id: str, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, filename, company_name, expiration_date, results_json, created_at FROM compliance_history WHERE id = ? AND user_id = ?", 
              (report_id, current_user["id"]))
    row = c.fetchone()
    conn.close()
    
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
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE compliance_history SET company_name = ?, expiration_date = ? WHERE id = ? AND user_id = ?",
              (req.company_name, req.expiration_date, report_id, current_user["id"]))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@router.delete("/compliance-history/{report_id}")
def delete_compliance_report(report_id: str, current_user: dict = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM compliance_history WHERE id = ? AND user_id = ?", (report_id, current_user["id"]))
    conn.commit()
    conn.close()
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
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT company_name, expiration_date FROM compliance_history WHERE id = ? AND user_id = ?", 
              (report_id, current_user["id"]))
    row = c.fetchone()
    conn.close()
    
    if not row or not row["expiration_date"]:
        raise HTTPException(status_code=404, detail="Expiration date not found for this report.")
        
    company_name = row["company_name"] or "Contract"
    expiry_date_str = row["expiration_date"]
    
    try:
        dt = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d")
        dt_start = dt.strftime("%Y%m%d")
        dt_end = (dt + datetime.timedelta(days=1)).strftime("%Y%m%d")
        now_str = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        
        ics_content = f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//LegalAnalyzer//ContractMonitor//EN\n"
        ics_content += f"BEGIN:VEVENT\nUID:{report_id}@legal-analyzer.com\nDTSTAMP:{now_str}\n"
        ics_content += f"DTSTART;VALUE=DATE:{dt_start}\nDTEND;VALUE=DATE:{dt_end}\n"
        ics_content += f"SUMMARY:Contract Expiry: {company_name}\n"
        ics_content += f"DESCRIPTION:Contract with {company_name} is expiring on {expiry_date_str}.\n"
        ics_content += f"END:VEVENT\nEND:VCALENDAR"

        return Response(content=ics_content, media_type="text/calendar", headers={
            "Content-Disposition": f"attachment; filename=contract_expiry_{company_name.replace(' ', '_')}.ics"
        })
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format in database")

