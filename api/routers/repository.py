from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
import os
from datetime import timezone
from pydantic import BaseModel
from typing import Optional
import auth
from services import pg_service
from services import grant_policy
from services.grant_policy import active_grant_sql, touch_grants

router = APIRouter(prefix="/api", tags=["repository"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")


def _basename_any(path: str) -> str:
    """os.path.basename that also strips Windows separators.

    The corpus DB stores local_path values scraped on a Windows machine
    (``C:\\...\\data\\pdfs\\kemenkeu\\x.pdf``). On the Linux backend
    os.path.basename does not treat ``\\`` as a separator and returns the
    whole path unchanged, which used to leak into the API's ``filename``
    field (bug_reports.md 2026-10-07). Normalize first, then basename.
    """
    return os.path.basename(path.replace("\\", "/"))


def _find_pdf_file(safe_filename: str) -> str | None:
    """Resolve a bare PDF filename against PDFS_DIR, including subfolders.

    The corpus keeps agency PDFs in subfolders (kemenkeu/, kemnaker/) while
    the API contract only carries the bare filename, so a top-level miss
    falls back to a deterministic walk (sorted) of PDFS_DIR. safe_filename
    must already be a basename (no separators) — guaranteed by callers.
    """
    top = os.path.join(PDFS_DIR, safe_filename)
    if os.path.isfile(top):
        return top
    for root, dirs, files in os.walk(PDFS_DIR):
        dirs.sort()
        if safe_filename in sorted(files):
            return os.path.join(root, safe_filename)
    return None

class AccessGrantRequest(BaseModel):
    granted_to: str
    reason: str
    expires_at: Optional[str] = None

# Statuses of upload-queue rows (pending-first flow): only the sekretaris
# perusahaan queue (role level 5+) may open these files.
_PENDING_STATUSES = ("Menunggu Konfirmasi", "Memproses")


def user_can_view_regulation(current_user: dict, reg_row: dict) -> bool:
    """Same visibility rules as GET /api/repository, for ONE regulation row.

    reg_row needs ``id``, ``klasifikasi`` and ``status``. Rules (plan D6):
    admin never (FR-24); queue rows (Menunggu Konfirmasi / Memproses) only at
    role level >= 5; otherwise klasifikasi Umum, plus Rahasia at level >= 2 and
    Terbatas at level >= 3, or an unexpired access_grants row for this user.
    """
    role = (current_user.get("role") or "pengguna").lower()
    if role == "admin":
        return False
    level = auth.get_role_level(role)
    if (reg_row.get("status") or "") in _PENDING_STATUSES:
        return level >= 5
    allowed = ["Umum"]
    if level >= 2:
        allowed.append("Rahasia")
    if level >= 3:
        allowed.append("Terbatas")
    # NULL klasifikasi matches nothing, exactly like the ANY(%s) filter in GET /api/repository.
    if reg_row.get("klasifikasi") in allowed:
        return True
    grant = pg_service.query_one(
        "SELECT 1 AS ok FROM access_grants "
        "WHERE doc_id = %s AND granted_to = %s "
        "AND " + active_grant_sql() + " LIMIT 1",
        (str(reg_row["id"]), current_user.get("id")))
    if grant is not None:
        # Grant was the deciding factor: record (throttled) last access.
        touch_grants(current_user.get("id"), [str(reg_row["id"])])
        return True
    return False


@router.get("/pdf/{filename}")
async def serve_pdf(filename: str, current_user: dict = Depends(auth.get_current_user)):
    """
    Return PDF bytes encoded as base64 JSON.
    IDM cannot intercept this because the Content-Type is application/json, not application/pdf.
    The frontend decodes the base64 and creates a blob:// URL to render in an iframe.

    Auth hardening (2026-10-14, D6): this used to serve any file in data/pdfs to
    anyone, including Rahasia/Terbatas documents. It now requires a login and
    applies the repository ACL (user_can_view_regulation) to the regulation
    whose local_path basename equals the requested name. A name that matches no
    regulation row is a 404 (never reveals whether the file exists on disk).
    """
    import base64
    if (current_user.get("role") or "").lower() == "admin":
        raise HTTPException(status_code=403, detail="Admin sistem tidak memiliki kewenangan untuk mengakses repositori dokumen.")
    safe_filename = _basename_any(filename)  # prevent path traversal
    # local_path values may hold Windows separators (see _basename_any), so the
    # basename is taken in SQL by stripping everything up to the last / or \.
    regs = pg_service.query(
        "SELECT id, klasifikasi, status FROM regulations "
        "WHERE local_path IS NOT NULL AND local_path <> '' "
        "AND regexp_replace(local_path, '^.*[\\\\/]', '') = %s",
        (safe_filename,))
    if not regs:
        raise HTTPException(status_code=404, detail=f"PDF '{safe_filename}' not found")
    if not any(user_can_view_regulation(current_user, r) for r in regs):
        raise HTTPException(status_code=403, detail="Anda tidak memiliki akses ke dokumen ini.")
    file_path = _find_pdf_file(safe_filename)
    if file_path is None:
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
        "SELECT id, judul, nomor, jenis, sektor, status, local_path, klasifikasi, "
        "ai_confidence, approval_mode "
        "FROM regulations "
        "WHERE local_path IS NOT NULL AND local_path != '' "
        "AND status != 'Menunggu Konfirmasi' "
        # Pending-first flow (2026-09-30): in-flight docs (scan/classify/ingest)
        # stay out of the designated tabs until resolved — uploads are tracked
        # in the pending queue + Riwayat Unggahan instead. Transient scraper
        # 'Memproses' rows are hidden too (decision A); System Monitoring still
        # shows their ACTIVE_TASKS progress.
        "AND status != 'Memproses' "
        "AND ( "
        "    klasifikasi = ANY(%s) "
        "    OR id::text IN ( "
        "        SELECT doc_id FROM access_grants "
        "        WHERE granted_to = %s "
        "        AND " + active_grant_sql() + " "
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
        filename = _basename_any(local_path) if local_path else None
        docs.append({
            "id": str(reg_id) if reg_id is not None else None,
            "judul": str(judul) if judul else "",
            "nomor": str(nomor) if nomor is not None else "",
            "jenis": str(jenis) if jenis else "",
            "sektor": str(sektor) if sektor else "",
            "status": str(status) if status else "",
            "klasifikasi": str(klasifikasi) if klasifikasi else "Umum",
            "filename": filename,
            # Provenance of the classification (migration 005): 'auto' =
            # confidence-gated auto-approval, 'manual' = pending confirmed,
            # None = pre-feature row.
            "ai_confidence": row["ai_confidence"],
            "approval_mode": row["approval_mode"]
        })
    return {"documents": docs}


@router.get("/repository/pending")
async def get_pending_repository(current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    # Pending-first flow (2026-09-30): the queue now covers the whole upload
    # lifecycle — 'Memproses' (scanning/classifying, read-only cards) plus
    # 'Menunggu Konfirmasi' (approval cards). Restricted to upload-originated
    # rows (nomor LIKE 'CUSTOM-%'); scraper transients are not queue items.
    records = pg_service.query(
        "SELECT r.id, r.judul, r.nomor, r.jenis, r.sektor, r.status, r.local_path, "
        "r.klasifikasi, r.ai_confidence, r.ai_jenis, r.ai_reasoning, h.uploaded_at "
        "FROM regulations r "
        "LEFT JOIN upload_history h ON h.doc_id = r.id::text "
        "WHERE r.status IN ('Memproses', 'Menunggu Konfirmasi') "
        "AND r.nomor LIKE 'CUSTOM-%' "
        "ORDER BY h.uploaded_at DESC NULLS LAST, r.id DESC")

    from services.app_state import ACTIVE_TASKS
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
        filename = _basename_any(local_path) if local_path else None
        doc_id_str = str(reg_id) if reg_id is not None else None
        scanning = status == "Memproses"
        # Decision D: a 'Memproses' row with no RUNNING task is stale (backend
        # restarted mid-scan) — flag it so the FE can offer a hint + reject.
        task = ACTIVE_TASKS.get(f"task_{doc_id_str}") if doc_id_str else None
        stale = scanning and not (task and task.get("status") in ("QUEUED", "RUNNING"))
        uploaded_at = row["uploaded_at"]
        docs.append({
            "id": doc_id_str,
            "judul": str(judul) if judul else "",
            "nomor": str(nomor) if nomor is not None else "",
            "jenis": str(jenis) if jenis else "",
            "sektor": str(sektor) if sektor else "",
            "status": str(status) if status else "",
            "klasifikasi": str(klasifikasi) if klasifikasi else "Umum",
            "filename": filename,
            # VLM classification verdict (migration 005) — null on rows that
            # predate the confidence-gated classifier.
            "ai_confidence": row["ai_confidence"],
            "ai_jenis": row["ai_jenis"] or "",
            "ai_reasoning": row["ai_reasoning"] or "",
            # Queue-stage fields (2026-09-30): 'scanning' = AI pass running,
            # 'awaiting_approval' = needs the sekretaris decision.
            "stage": "scanning" if scanning else "awaiting_approval",
            "stale": stale,
            "uploaded_at": uploaded_at.isoformat() if uploaded_at else None
        })
    return {"documents": docs}


@router.get("/repository/uploads/history")
async def get_upload_history(
    limit: int = 50,
    offset: int = 0,
    outcome: Optional[str] = None,
    current_user: dict = Depends(auth.require_role("manajer"))
):
    """Riwayat Unggahan (2026-09-30, migration 006): durable upload trail.

    Visibility (decision B): sekretaris perusahaan sees every upload; manajer
    and direktur see only their own. Admin is blocked, mirroring the FR-24
    repository exclusion."""
    role = (current_user.get("role") or "").lower()
    if role == "admin":
        raise HTTPException(status_code=403, detail="Admin sistem tidak memiliki kewenangan untuk mengakses repositori dokumen.")

    limit = max(1, min(200, limit))
    offset = max(0, offset)
    where, params = [], []
    # dewa (unofficial developer role) gets sekretaris-level full visibility.
    if role not in ("sekretaris perusahaan", "dewa"):
        where.append("uploaded_by = %s")
        params.append(str(current_user.get("id", "")))
    if outcome:
        where.append("outcome = %s")
        params.append(outcome)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = pg_service.query_one(
        f"SELECT count(*) AS n FROM upload_history {where_sql}", tuple(params))["n"]
    rows = pg_service.query(
        f"SELECT id, doc_id, filename, judul, uploaded_by_name, uploaded_at, "
        f"outcome, ai_klasifikasi, ai_jenis, ai_confidence, final_klasifikasi, "
        f"final_jenis, resolved_at, resolved_by_name, detail "
        f"FROM upload_history {where_sql} "
        f"ORDER BY uploaded_at DESC, id DESC LIMIT %s OFFSET %s",
        tuple(params + [limit, offset]))
    entries = [{
        "id": r["id"],
        "doc_id": r["doc_id"],
        "filename": r["filename"],
        "judul": r["judul"] or "",
        "uploaded_by_name": r["uploaded_by_name"] or "",
        "uploaded_at": r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
        "outcome": r["outcome"],
        "ai_klasifikasi": r["ai_klasifikasi"] or "",
        "ai_jenis": r["ai_jenis"] or "",
        "ai_confidence": r["ai_confidence"],
        "final_klasifikasi": r["final_klasifikasi"] or "",
        "final_jenis": r["final_jenis"] or "",
        "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        "resolved_by_name": r["resolved_by_name"] or "",
        "detail": r["detail"] or ""
    } for r in rows]
    return {"total": total, "entries": entries}

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
        
    # Defense-in-depth: every visibility check joins access_grants.granted_to
    # against users.id (TEXT UUID), so a grant to a non-existent user is a
    # silent dead grant — reject it at the source instead of storing it.
    grantee = pg_service.query_one("SELECT id FROM users WHERE id = %s", (req.granted_to,))
    if not grantee:
        raise HTTPException(status_code=400, detail="Pengguna penerima akses tidak ditemukan.")
        
    grant_id = str(uuid.uuid4())
    # expires_at: '' (legacy empty string) becomes NULL -- TIMESTAMPTZ cannot
    # store empty strings.
    # Grant row and its 'granted' history event commit together (migration 012):
    # a failed event rolls the grant back so a retry cannot duplicate it.
    with pg_service.get_conn() as conn:
        conn.execute(
            "INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (grant_id, doc_id, current_user["id"], req.granted_to, req.reason, req.expires_at or None))
        grantee_row = conn.execute(
            "SELECT username FROM users WHERE id = %s", (req.granted_to,)).fetchone()
        grant_policy.record_event(
            conn,
            {"id": grant_id, "doc_id": doc_id, "granted_to": req.granted_to,
             "grantee_name": grantee_row["username"] if grantee_row else None,
             "judul": doc["judul"]},
            "granted", current_user)
    
    # FR-25: Audit log the grant (the reason is deliberately NOT written here:
    # it may be hidden per grant; it lives only in access_grants.reason).
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "GRANT_ACCESS", doc_id, 
              f"Diberikan kepada: {req.granted_to}, Dokumen: {doc_judul}")
    
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
        "SELECT ag.id, ag.doc_id, ag.granted_by, ag.granted_to, "
        "CASE WHEN ag.reason_visible THEN ag.reason ELSE NULL END AS reason, "
        "NOT ag.reason_visible AS reason_hidden, "
        "to_char(ag.expires_at, 'YYYY-MM-DD HH24:MI:SS') AS expires_at, "
        "to_char(ag.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at, "
        "r.judul, r.klasifikasi "
        "FROM access_grants ag "
        "LEFT JOIN regulations r ON ag.doc_id = r.id::text ")
    
    # FR-23: Sekretaris Perusahaan sees everything; others see only what they granted
    if user_level >= 5:  # Sekretaris Perusahaan
        rows = pg_service.query(
            base_sql + "WHERE ag.revoked_at IS NULL ORDER BY ag.created_at DESC")
    else:
        rows = pg_service.query(
            base_sql + "WHERE ag.revoked_at IS NULL AND ag.granted_by = %s "
            "ORDER BY ag.created_at DESC",
            (current_user["id"],))
    
    return {"grants": rows}

@router.delete("/repository/grant/{grant_id}")
async def revoke_grant(grant_id: str, current_user: dict = Depends(auth.get_current_user)):
    """FR-23: Sekretaris Perusahaan can revoke any grant."""
    user_level = auth.get_role_level(current_user.get("role", "pengguna"))
    if user_level < 5:  # Only Sekretaris Perusahaan
        raise HTTPException(status_code=403, detail="Hanya Sekretaris Perusahaan yang dapat mencabut pemberian akses.")
    
    if not grant_policy.is_grant_id(grant_id):
        raise HTTPException(status_code=404, detail="Grant tidak ditemukan.")

    # Soft revoke (migration 012): the row stays for history. Idempotent
    # guard: only the first call revokes; later calls get 409 and write nothing.
    with pg_service.get_conn() as conn:
        row = conn.execute(
            "SELECT ag.id, ag.doc_id, ag.granted_to, ag.expires_at, ag.revoked_at, "
            "ru.username AS grantee_name, r.judul "
            "FROM access_grants ag "
            "LEFT JOIN users ru ON ru.id = ag.granted_to "
            "LEFT JOIN regulations r ON ag.doc_id = r.id::text "
            "WHERE ag.id = %s FOR UPDATE OF ag", (grant_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Grant tidak ditemukan.")
        if row["revoked_at"] is not None:
            raise HTTPException(status_code=409, detail="Akses sudah dicabut.")
        conn.execute(
            "UPDATE access_grants SET revoked_at = NOW(), revoked_by = %s WHERE id = %s",
            (current_user["id"], grant_id))
        exp = row["expires_at"]
        old = exp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if exp else "none"
        grant_policy.record_event(conn, row, "revoked", current_user, old=old)
    
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "REVOKE_ACCESS", row["doc_id"], f"Akses dicabut dari: {row['granted_to']}")
    return {"message": "Akses berhasil dicabut."}

def _classify_threshold() -> int:
    """Confidence gate for VLM auto-classification (2026-09-30): a doc
    scoring >= CLASSIFY_CONFIDENCE_THRESHOLD skips human pending review and
    is ingested immediately with the AI labels; below it (or on a failed
    classifier run) it goes to 'Menunggu Konfirmasi' as before. The env var
    is read per run, so a threshold change only needs a backend restart."""
    try:
        return max(0, min(100, int(os.getenv("CLASSIFY_CONFIDENCE_THRESHOLD", "80"))))
    except ValueError:
        return 80

def process_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    from services import task_queue
    retry_args = (file_path, doc_id, filename, nomor, jenis, sektor, status, klasifikasi)
    task_id = task_queue.start(doc_id, "parse", f"Parsing (AI Review): {filename}", filename, retry_args)
    if task_id is None:
        return  # cancelled while still queued
    try:
        if task_queue.ACTIVE_TASKS[task_id]["attempts"] > 1:
            # Retry: the row went to 'Gagal - Error' last time; show it as in-flight again.
            pg_service.execute(
                "UPDATE regulations SET status = 'Memproses' WHERE id::text = %s", (doc_id,))
        # (R1 bugfix: hybrid PyMuPDF+VLM extraction replaces the dead
        # PaddleOCR chain; see la-legpro-doc/bug_reports.md.)
        from services.rag_service import extract_text_hybrid, classify_document_vlm
        task_queue.begin_stage(task_id, 0)
        full_text = extract_text_hybrid(file_path)

        # ── AI scan + classification (FR-4; confidence-gated 2026-09-30) ────
        # Replaces the old one-word secrecy recommendation: one VLM pass over
        # page 1 + extracted text returns {klasifikasi, jenis, confidence,
        # reasoning}; classifier failures come back with confidence 0.
        task_queue.begin_stage(task_id, 1)
        ai = classify_document_vlm(file_path, full_text)
        threshold = _classify_threshold()
        pg_service.execute(
            "UPDATE regulations SET klasifikasi = %s, ai_jenis = %s, "
            "ai_confidence = %s, ai_reasoning = %s WHERE id::text = %s",
            (ai["klasifikasi"], ai["jenis"], ai["confidence"], ai["reasoning"], doc_id))

        from services.db_service import record_upload_event
        if ai["jenis"] and ai["confidence"] >= threshold:
            # High-confidence path: fully automatic — adopt both AI labels,
            # mark the row auditable as auto-approved, and ingest right away.
            pg_service.execute(
                "UPDATE regulations SET jenis = %s, approval_mode = 'auto' WHERE id::text = %s",
                (ai["jenis"], doc_id))
            record_upload_event(doc_id, "auto_approved", ai=ai,
                                final_klasifikasi=ai["klasifikasi"], final_jenis=ai["jenis"])
            print(f"Document {doc_id} auto-approved (confidence {ai['confidence']} >= {threshold}): "
                  f"{ai['klasifikasi']}/{ai['jenis']}")
            task_queue.complete(task_id)
            # Same worker thread: ingest re-registers task_id with its own
            # RUNNING→COMPLETED lifecycle (chained: attempts/uploader carry
            # over), so the progress UI stays truthful.
            ingest_document_background(file_path, doc_id, filename, nomor,
                                       ai["jenis"], sektor, "Berlaku", ai["klasifikasi"],
                                       _chained=True)
            return

        # Low confidence (or classifier error): human approval required.
        pg_service.execute(
            "UPDATE regulations SET status = 'Menunggu Konfirmasi' WHERE id::text = %s",
            (doc_id,))
        record_upload_event(doc_id, "awaiting_approval", ai=ai)
        print(f"Document {doc_id} set to Pending Confirmation (confidence {ai['confidence']} < {threshold}, "
              f"AI recommendation: {ai['klasifikasi']}/{ai['jenis']})")
        task_queue.complete(task_id)

    except task_queue.TaskCancelled:
        _mark_cancelled(task_id, doc_id, "Parsing/klasifikasi")
    except Exception as e:
        task_queue.fail(task_id, e)
        print(f"Error in process_document_background: {e}")
        try:
            pg_service.execute(
                "UPDATE regulations SET status = 'Gagal - Error' WHERE id::text = %s", (doc_id,))
        except:
            pass
        try:
            from services.db_service import record_upload_event
            record_upload_event(doc_id, "failed_error", detail=f"Parsing/klasifikasi gagal: {e}")
        except:
            pass

def _mark_cancelled(task_id: str, doc_id: str, phase: str):
    """Operator cancelled the task at a stage boundary: close the task and
    leave the row in a cleanable 'Gagal - Dibatalkan' state (matched by the
    existing LIKE 'Gagal%' cleanup)."""
    from services import task_queue
    task_queue.cancelled(task_id)
    try:
        pg_service.execute(
            "UPDATE regulations SET status = 'Gagal - Dibatalkan' WHERE id::text = %s", (doc_id,))
    except Exception:
        pass
    try:
        from services.db_service import record_upload_event
        record_upload_event(doc_id, "failed_error", detail=f"{phase} dibatalkan oleh operator.")
    except Exception:
        pass

def ingest_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str, _chained: bool = False):
    from services import task_queue
    retry_args = (file_path, doc_id, filename, nomor, jenis, sektor, status, klasifikasi)
    task_id = task_queue.start(doc_id, "ingest", f"Ingesting (Vector+KG): {filename}", filename,
                               retry_args, chained=_chained)
    if task_id is None:
        return  # cancelled while still queued
    try:
        if task_queue.ACTIVE_TASKS[task_id]["attempts"] > 1 and not _chained:
            pg_service.execute(
                "UPDATE regulations SET status = 'Memproses' WHERE id::text = %s", (doc_id,))
        from pdf_parser import LegalChunker
        from services.rag_service import extract_text_hybrid
        chunker = LegalChunker()
        
        # (R1 bugfix: hybrid PyMuPDF+VLM extraction replaces the dead
        # PaddleOCR chain; see la-legpro-doc/bug_reports.md.)
        task_queue.begin_stage(task_id, 0)
        full_text = extract_text_hybrid(file_path)
        
        # Duplicate Detection (FR-5)
        task_queue.begin_stage(task_id, 1)
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
                # History (2026-09-30): overwrites auto_approved/confirmed when
                # the ingest itself fails on a near-duplicate — truthful outcome.
                from services.db_service import record_upload_event
                record_upload_event(doc_id, "failed_duplicate",
                                    detail=f"Jarak kemiripan {dist:.3f} < 0.15 terhadap korpus yang ada.")
                # Permanent: the source file is gone and a retry would fail the same way.
                task_queue.fail(task_id, "Dokumen duplikat: terlalu mirip dengan dokumen yang sudah ada.",
                                retryable=False)
                return

        print(f"File saved to DB. Now parsing and embedding: {filename}")
        
        # Contextual Enrichment - Generate Global Document Summary
        task_queue.begin_stage(task_id, 2)
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
        
        task_queue.begin_stage(task_id, 3)
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
            task_queue.begin_stage(task_id, 4)
        except task_queue.TaskCancelled:
            # The doc is already fully searchable ('Berlaku'); only the graph is
            # skipped, so this is a clean finish, not a failed document.
            task_queue.complete(task_id)
            return
        try:
            from services.kg_service import extract_and_store_graph
            extract_and_store_graph(doc_id, full_text, nomor, judul, jenis)
        except Exception as kg_err:
            print(f"[KG] Non-fatal extraction error for {nomor}: {kg_err}")
            
        task_queue.complete(task_id)
        return
        
    except task_queue.TaskCancelled:
        _mark_cancelled(task_id, doc_id, "Ingest")
    except Exception as e:
        task_queue.fail(task_id, e)
        print(f"Error processing PDF: {e}")
        try:
            pg_service.execute(
                "UPDATE regulations SET status = 'Gagal - Error' WHERE id::text = %s", (doc_id,))
        except:
            pass
        try:
            from services.db_service import record_upload_event
            record_upload_event(doc_id, "failed_error", detail=f"Ingest gagal: {e}")
        except:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# Knowledge Graph Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.delete("/repository/document/{doc_id}")
async def delete_document(doc_id: str, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    """Deletes a document from the repository."""
    
    # Check if doc exists (status decides the history outcome below: deleting
    # a still-pending upload is a REJECTION, anything else is a plain delete)
    row = pg_service.query_one(
        "SELECT local_path, judul, status FROM regulations WHERE id::text = %s", (doc_id,))
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
    if task and task.get("status") in ("QUEUED", "RUNNING"):
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
    from services.db_service import log_audit, record_upload_event
    log_audit(current_user.get("id", ""), "DELETE_DOCUMENT", doc_id, f"Menghapus dokumen: {judul}")

    # 7. Upload history (no-op UPDATE for non-upload docs — they have no row):
    # pending-queue deletes are rejections; everything else is a plain delete.
    # 'Memproses' rows can only be deleted when stale (the M4 guard above
    # rejects while the task is RUNNING), so deleting one is also a rejection.
    outcome = "rejected" if row["status"] in ("Menunggu Konfirmasi", "Memproses") else "deleted"
    record_upload_event(doc_id, outcome, resolver=current_user)
 
    return {"message": "Dokumen berhasil dihapus."}

# ─────────────────────────────────────────────────────────────────────────────
# Pending confirmation & failed-document cleanup endpoints
# (moved verbatim from api/main.py during the Phase 2 refactor — same paths,
#  same auth; /api prefix comes from this router)
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmPendingRequest(BaseModel):
    klasifikasi: str
    # Approved taxonomy label (2026-09-30): the pending UI now offers the AI's
    # ai_jenis pre-selected in a taxonomy dropdown; omit to keep the current
    # jenis on the row.
    jenis: Optional[str] = None

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

    # The approved jenis must come from the active taxonomy (the FE select is
    # fed from /api/taxonomy; this keeps the endpoint honest for other clients).
    jenis_final = (req.jenis or "").strip() or jenis
    if req.jenis:
        tax = pg_service.query_one(
            "SELECT name FROM document_taxonomy WHERE name = %s AND is_active = TRUE",
            (jenis_final,))
        if not tax:
            raise HTTPException(status_code=400, detail="Jenis dokumen tidak ada dalam taksonomi aktif.")

    # Update DB — approval_mode='manual' records that a human confirmed this
    # doc through the pending queue (vs 'auto' for confidence-gated approvals).
    pg_service.execute(
        "UPDATE regulations SET status = 'Berlaku', klasifikasi = %s, jenis = %s, "
        "approval_mode = 'manual' WHERE id::text = %s",
        (req.klasifikasi, jenis_final, doc_id))

    from services.db_service import log_audit, record_upload_event
    log_audit(current_user.get("id", ""), "CONFIRM_DOCUMENT", doc_id,
              f"Mengonfirmasi dokumen pending: {judul} (klasifikasi={req.klasifikasi}, jenis={jenis_final})")
    record_upload_event(doc_id, "confirmed",
                        final_klasifikasi=req.klasifikasi, final_jenis=jenis_final,
                        resolver=current_user)

    # Trigger vector DB injection and KG asynchronously
    filename = os.path.basename(local_path)
    from services import task_queue
    ingest_args = (local_path, doc_id, filename, nomor, jenis_final, sektor, 'Berlaku', req.klasifikasi)
    task_queue.enqueue(doc_id, "ingest", f"Ingesting (Vector+KG): {filename}", filename, ingest_args)
    background_tasks.add_task(ingest_document_background, *ingest_args, _chained=True)
    
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
