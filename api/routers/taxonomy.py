from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from psycopg import errors as pg_errors

from auth import get_current_user
from services import pg_service

# Migration M3 (cutover): document_taxonomy lives in PostgreSQL; the local
# sqlite3 connection helper is replaced by services.pg_service.

router = APIRouter()

class TaxonomyCreate(BaseModel):
    name: str

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
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Nama taksonomi tidak boleh kosong")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"Nama taksonomi maksimal {MAX_NAME_LEN} karakter")
    return name

@router.get("/taxonomy")
def get_taxonomy(current_user: dict = Depends(get_current_user)):
    # usage_count (2026-10-06): repository documents classified under each
    # jenis, so the FE delete/deactivate dialog can state consequences.
    # Additive field; existing consumers only read id/name/is_active.
    rows = pg_service.query(
        "SELECT t.id, t.name, t.is_active, COUNT(r.id) AS usage_count "
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
        }
        for r in rows
    ]}

@router.post("/taxonomy")
def create_taxonomy(req: TaxonomyCreate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    try:
        pg_service.execute(
            "INSERT INTO document_taxonomy (name, is_active) VALUES (%s, TRUE)",
            (normalize_name(req.name),)
        )
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Taksonomi dengan nama tersebut sudah ada")
    return {"message": "Taksonomi berhasil ditambahkan"}

@router.put("/taxonomy/{tax_id}")
def update_taxonomy(tax_id: int, req: TaxonomyUpdate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    # Normalize before opening the transaction so validation errors never
    # reach the connection block.
    new_name = normalize_name(req.name) if req.name is not None else None
    # Single transaction so a name+is_active update is atomic (the sqlite
    # version committed both statements together as well).
    try:
        with pg_service.get_conn() as conn:
            if new_name is not None:
                conn.execute(
                    "UPDATE document_taxonomy SET name = %s WHERE id = %s",
                    (new_name, tax_id)
                )
            if req.is_active is not None:
                conn.execute(
                    "UPDATE document_taxonomy SET is_active = %s WHERE id = %s",
                    (req.is_active, tax_id)
                )
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Taksonomi dengan nama tersebut sudah ada")
    return {"message": "Taksonomi berhasil diperbarui"}

@router.delete("/taxonomy/{tax_id}")
def delete_taxonomy(tax_id: int, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    # Usage-based dual gate (2026-10-06, user decision): sekretaris perusahaan
    # may hard-delete only unused types; deleting a type that still labels
    # repository documents requires admin / insinyur ti. Deactivation
    # (PUT is_active=false) stays the reversible path for every writer role.
    # The FE mirrors this rule for button state; this check is the authority.
    role = (current_user.get("role") or "").lower()
    if role not in ["admin", "insinyur ti", "dewa"]:
        row = pg_service.query_one(
            "SELECT COUNT(*) AS n FROM document_taxonomy t "
            "JOIN regulations r ON r.jenis = t.name WHERE t.id = %s",
            (tax_id,)
        )
        if row and int(row["n"]) > 0:
            raise HTTPException(
                status_code=403,
                detail="Jenis ini masih digunakan oleh dokumen di repositori. "
                       "Nonaktifkan sebagai gantinya, atau hubungi administrator "
                       "untuk menghapus permanen."
            )
    pg_service.execute("DELETE FROM document_taxonomy WHERE id = %s", (tax_id,))
    return {"message": "Taksonomi berhasil dihapus"}
