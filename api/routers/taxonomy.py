from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from psycopg import errors as pg_errors

from auth import get_current_user
from services import pg_service
from services.db_service import log_audit

# Migration M3 (cutover): document_taxonomy lives in PostgreSQL; the local
# sqlite3 connection helper is replaced by services.pg_service.

router = APIRouter()

class TaxonomyCreate(BaseModel):
    name: str
    # Faithful undo restore (2026-10-12, critique Issue 2): the FE "Urungkan"
    # action re-POSTs a deleted row — a deleted *inactive* type must not come
    # back active. Additive optional field; None keeps the historical TRUE
    # default, so every existing consumer stays byte-identical.
    is_active: Optional[bool] = None

class TaxonomyUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None

def check_permission(user: dict):
    role = (user.get("role") or "").lower()
    if role not in ["sekretaris perusahaan", "admin", "insinyur ti", "dewa"]:
        raise HTTPException(status_code=403, detail="Akses ditolak")

# Hardening (2026-10-06): document_taxonomy.name has no DB length cap and the
# FE inputs cap at 100 characters — enforce the same cap server-side for direct
# API clients, and reject blank names instead of storing "".
MAX_NAME_LEN = 100

def normalize_name(raw: str) -> str:
    # Terminology (2026-10-08, critique minor 3): these details surface in FE
    # error lanes, where the documented object noun is "jenis dokumen" —
    # "Taksonomi" stays the screen/collection identity only (ui_ux_design.md).
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nama jenis dokumen tidak boleh kosong")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"Nama jenis dokumen maksimal {MAX_NAME_LEN} karakter")
    return name

@router.get("/taxonomy")
def get_taxonomy(current_user: dict = Depends(get_current_user)):
    # usage_count (2026-10-06): repository documents classified under each
    # jenis, so the FE delete/deactivate dialog can state consequences.
    # internal_count (2026-10-08): the subset the Legal Repository shows on
    # its "Dokumen Internal" tab — mirrors LegalRepository.tsx's sektor split
    # so the FE usage link can target the right ?tab= deep-link param.
    # Additive fields; existing consumers only read id/name/is_active.
    rows = pg_service.query(
        "SELECT t.id, t.name, t.is_active, COUNT(r.id) AS usage_count, "
        "COUNT(r.id) FILTER (WHERE r.sektor IN ('Upload Manual', 'Dokumen Internal')) "
        "AS internal_count "
        "FROM document_taxonomy t "
        "LEFT JOIN regulations r ON r.jenis = t.name "
        "GROUP BY t.id, t.name, t.is_active "
        "ORDER BY t.name ASC"
    )
    return {"taxonomy": [
        {
            "id": r["id"],
            "name": r["name"],
            "is_active": bool(r["is_active"]),
            "usage_count": int(r["usage_count"]),
            "internal_count": int(r["internal_count"]),
        }
        for r in rows
    ]}

@router.post("/taxonomy")
def create_taxonomy(req: TaxonomyCreate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    name = normalize_name(req.name)
    try:
        rows = pg_service.execute_returning(
            "INSERT INTO document_taxonomy (name, is_active) VALUES (%s, %s) RETURNING id",
            (name, True if req.is_active is None else req.is_active)
        )
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Jenis dokumen dengan nama tersebut sudah ada")
    # Audit trail (2026-10-08, critique Issue 2): every taxonomy mutation is
    # attributable — log_audit never raises and runs on its own connection.
    log_audit(current_user["id"], "CREATE_TAXONOMY",
              str(rows[0]["id"]) if rows else "", f'"{name}"')
    return {"message": "Jenis dokumen berhasil ditambahkan"}

@router.put("/taxonomy/{tax_id}")
def update_taxonomy(tax_id: int, req: TaxonomyUpdate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    # Normalize before opening the transaction so validation errors never
    # reach the connection block.
    new_name = normalize_name(req.name) if req.name is not None else None
    # Cascade rename (2026-10-08 harden, critique Issue 1, user decision — the
    # id-based FK alternative was rejected): regulations.jenis name-joins
    # document_taxonomy.name, so a rename must carry its documents along or it
    # silently detaches them (usage_count → 0, classifications orphaned). Both
    # UPDATEs share one transaction — a UniqueViolation rolls back name and
    # cascade together, never a half-renamed state. FOR UPDATE serializes
    # concurrent renames: the loser cascades from the winner's fresh name and
    # its stale-name WHERE matches 0 rows, leaving a consistent end state.
    # Post-cascade, DELETE remains the only detaching operation.
    updated_documents = 0
    old_name = None
    renamed = False
    try:
        with pg_service.get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM document_taxonomy WHERE id = %s FOR UPDATE",
                (tax_id,)
            ).fetchone()
            old_name = row["name"] if row else None
            if new_name is not None and old_name is not None and old_name != new_name:
                conn.execute(
                    "UPDATE document_taxonomy SET name = %s WHERE id = %s",
                    (new_name, tax_id)
                )
                casc = conn.execute(
                    "UPDATE regulations SET jenis = %s WHERE jenis = %s",
                    (new_name, old_name)
                )
                updated_documents = casc.rowcount
                renamed = True
            if req.is_active is not None and old_name is not None:
                conn.execute(
                    "UPDATE document_taxonomy SET is_active = %s WHERE id = %s",
                    (req.is_active, tax_id)
                )
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Jenis dokumen dengan nama tersebut sudah ada")
    # Audit after commit; a PUT on a missing id changed nothing, so it logs
    # nothing (old_name is None only when the row does not exist).
    if old_name is not None:
        if renamed:
            log_audit(current_user["id"], "RENAME_TAXONOMY", str(tax_id),
                      f'"{old_name}" -> "{new_name}" ({updated_documents} dokumen mengikuti)')
        if req.is_active is not None:
            log_audit(current_user["id"],
                      "ACTIVATE_TAXONOMY" if req.is_active else "DEACTIVATE_TAXONOMY",
                      str(tax_id), f'"{new_name if renamed else old_name}"')
    return {"message": "Jenis dokumen berhasil diperbarui",
            "updated_documents": updated_documents}

@router.delete("/taxonomy/{tax_id}")
def delete_taxonomy(tax_id: int, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    # Usage-based dual gate (2026-10-06, user decision): sekretaris perusahaan
    # may hard-delete only unused types; deleting a type that still labels
    # repository documents requires admin / insinyur ti. Deactivation
    # (PUT is_active=false) stays the reversible path for every writer role.
    # The FE mirrors this rule for button state; this check is the authority.
    # Name + count are fetched for EVERY role (2026-10-08): the gate consumes
    # the count and the audit row records the blast radius — post-cascade,
    # delete is the only detaching operation (orphaned classifications
    # re-attach if the same name is recreated).
    row = pg_service.query_one(
        "SELECT t.name, COUNT(r.id) AS n "
        "FROM document_taxonomy t "
        "LEFT JOIN regulations r ON r.jenis = t.name "
        "WHERE t.id = %s GROUP BY t.id, t.name",
        (tax_id,)
    )
    role = (current_user.get("role") or "").lower()
    if role not in ["admin", "insinyur ti", "dewa"]:
        if row and int(row["n"]) > 0:
            raise HTTPException(
                status_code=403,
                detail="Jenis ini masih digunakan oleh dokumen di repositori. "
                       "Nonaktifkan sebagai gantinya, atau hubungi administrator "
                       "untuk menghapus permanen."
            )
    pg_service.execute("DELETE FROM document_taxonomy WHERE id = %s", (tax_id,))
    if row:
        log_audit(current_user["id"], "DELETE_TAXONOMY", str(tax_id),
                  f'"{row["name"]}" ({int(row["n"])} dokumen kehilangan klasifikasi jenis)')
    return {"message": "Jenis dokumen berhasil dihapus"}
