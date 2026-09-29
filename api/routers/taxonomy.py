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
    if role not in ["sekretaris perusahaan", "admin", "insinyur ti"]:
        raise HTTPException(status_code=403, detail="Akses ditolak")

@router.get("/taxonomy")
def get_taxonomy(current_user: dict = Depends(get_current_user)):
    rows = pg_service.query("SELECT id, name, is_active FROM document_taxonomy ORDER BY name ASC")
    return {"taxonomy": [{"id": r["id"], "name": r["name"], "is_active": bool(r["is_active"])} for r in rows]}

@router.post("/taxonomy")
def create_taxonomy(req: TaxonomyCreate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    try:
        pg_service.execute(
            "INSERT INTO document_taxonomy (name, is_active) VALUES (%s, TRUE)",
            (req.name,)
        )
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Taksonomi dengan nama tersebut sudah ada")
    return {"message": "Taksonomi berhasil ditambahkan"}

@router.put("/taxonomy/{tax_id}")
def update_taxonomy(tax_id: int, req: TaxonomyUpdate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    # Single transaction so a name+is_active update is atomic (the sqlite
    # version committed both statements together as well).
    try:
        with pg_service.get_conn() as conn:
            if req.name is not None:
                conn.execute(
                    "UPDATE document_taxonomy SET name = %s WHERE id = %s",
                    (req.name, tax_id)
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
    pg_service.execute("DELETE FROM document_taxonomy WHERE id = %s", (tax_id,))
    return {"message": "Taksonomi berhasil dihapus"}
