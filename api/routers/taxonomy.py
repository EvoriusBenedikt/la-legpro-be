from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import sqlite3

import os
from auth import get_current_user

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE_DIR, "data", "legal_metadata.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, is_active FROM document_taxonomy ORDER BY name ASC")
    rows = c.fetchall()
    conn.close()
    
    return {"taxonomy": [{"id": r["id"], "name": r["name"], "is_active": bool(r["is_active"])} for r in rows]}

@router.post("/taxonomy")
def create_taxonomy(req: TaxonomyCreate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO document_taxonomy (name, is_active) VALUES (?, 1)", (req.name,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Taksonomi dengan nama tersebut sudah ada")
    conn.close()
    return {"message": "Taksonomi berhasil ditambahkan"}

@router.put("/taxonomy/{tax_id}")
def update_taxonomy(tax_id: int, req: TaxonomyUpdate, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    conn = get_db_connection()
    c = conn.cursor()
    
    if req.name is not None:
        try:
            c.execute("UPDATE document_taxonomy SET name = ? WHERE id = ?", (req.name, tax_id))
        except sqlite3.IntegrityError:
            conn.close()
            raise HTTPException(status_code=400, detail="Taksonomi dengan nama tersebut sudah ada")
            
    if req.is_active is not None:
        c.execute("UPDATE document_taxonomy SET is_active = ? WHERE id = ?", (1 if req.is_active else 0, tax_id))
        
    conn.commit()
    conn.close()
    return {"message": "Taksonomi berhasil diperbarui"}

@router.delete("/taxonomy/{tax_id}")
def delete_taxonomy(tax_id: int, current_user: dict = Depends(get_current_user)):
    check_permission(current_user)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM document_taxonomy WHERE id = ?", (tax_id,))
    conn.commit()
    conn.close()
    return {"message": "Taksonomi berhasil dihapus"}
