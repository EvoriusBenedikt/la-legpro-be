from fastapi import APIRouter, Depends, HTTPException
import sqlite3
import os
from typing import List
from pydantic import BaseModel
import auth

router = APIRouter(prefix="/api/admin", tags=["admin"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class KGExclusion(BaseModel):
    entity_name: str

@router.get("/dashboard")
async def admin_dashboard(current_user: dict = Depends(auth.require_role("admin"))):
    """FR-26: Admin-only dashboard with system stats and audit logs."""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 1. Document Processing Status & Details
    c.execute("SELECT id, judul, status, nomor FROM regulations")
    all_docs = c.fetchall()
    
    doc_details = {
        "Berlaku": [],
        "Tidak Berlaku": [],
        "Memproses": [],
        "Gagal": []
    }
    
    for d in all_docs:
        s = (d["status"] or "").strip()
        doc_obj = {"id": d["id"], "judul": d["judul"], "nomor": d["nomor"], "status": s}
        
        if s == "Memproses":
            doc_details["Memproses"].append(doc_obj)
        elif s.startswith("Gagal"):
            doc_details["Gagal"].append(doc_obj)
        elif s == "Tidak Berlaku" or "Dicabut" in s:
            doc_details["Tidak Berlaku"].append(doc_obj)
        else:
            doc_details["Berlaku"].append(doc_obj)
            
    doc_status = {
        "Berlaku": len(doc_details["Berlaku"]),
        "Tidak Berlaku": len(doc_details["Tidak Berlaku"]),
        "Memproses": len(doc_details["Memproses"]),
        "Gagal": len(doc_details["Gagal"])
    }

    # 2. Document Volume by Klasifikasi
    c.execute("SELECT klasifikasi, COUNT(*) as count FROM regulations WHERE klasifikasi IS NOT NULL GROUP BY klasifikasi")
    klas_rows = c.fetchall()
    doc_by_klasifikasi = {r["klasifikasi"]: r["count"] for r in klas_rows}

    # 3. Document Volume by Jenis
    c.execute("SELECT jenis, COUNT(*) as count FROM regulations GROUP BY jenis ORDER BY count DESC LIMIT 10")
    jenis_rows = c.fetchall()
    doc_by_jenis = [dict(r) for r in jenis_rows]

    # 4. Active Access Grants count
    c.execute("SELECT COUNT(*) FROM access_grants WHERE expires_at IS NULL OR expires_at = '' OR expires_at >= datetime('now')")
    active_grants = c.fetchone()[0]

    # 5. Recent Audit Logs (last 100)
    c.execute("SELECT id, timestamp, user_id, action_type, resource_id, details FROM audit_logs ORDER BY id DESC LIMIT 100")
    audit_rows = c.fetchall()
    audit_logs = [dict(r) for r in audit_rows]

    conn.close()

    # 6. System Health
    chroma_ok = False
    try:
        import chromadb
        client = chromadb.PersistentClient(path=os.path.join(BASE_DIR, "data", "chroma_db"))
        col = client.get_or_create_collection("ojk_regulations")
        chroma_ok = col.count() >= 0
    except Exception as e:
        print(f"Chroma health check failed: {e}")

    return {
        "doc_status": doc_status,
        "doc_details": doc_details,
        "doc_by_klasifikasi": doc_by_klasifikasi,
        "doc_by_jenis": doc_by_jenis,
        "active_grants": active_grants,
        "audit_logs": audit_logs,
        "system_health": {
            "sqlite": True,
            "chromadb": chroma_ok
        }
    }

@router.get("/kg-exclusions")
async def get_kg_exclusions(current_user: dict = Depends(auth.require_role("admin"))):
    """FR-30: Get all entity exclusions"""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT id, entity_name, created_at FROM kg_exclusions ORDER BY created_at DESC")
    exclusions = [{"id": row[0], "entity_name": row[1], "created_at": row[2]} for row in c.fetchall()]
    conn.close()
    return {"exclusions": exclusions}

@router.post("/kg-exclusions")
async def add_kg_exclusion(req: KGExclusion, current_user: dict = Depends(auth.require_role("admin"))):
    """FR-30: Add entity exclusion and optionally delete existing nodes"""
    import sqlite3
    
    entity_name = req.entity_name.strip()
    if not entity_name:
        raise HTTPException(status_code=400, detail="Entity name is required")
        
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    try:
        # Add to exclusion list
        c.execute("INSERT INTO kg_exclusions (entity_name) VALUES (?)", (entity_name,))
        
        # Auto-cleanup: Delete any existing nodes with this exact label (case-insensitive)
        c.execute("SELECT id FROM kg_nodes WHERE LOWER(label) = LOWER(?)", (entity_name,))
        nodes_to_delete = [row[0] for row in c.fetchall()]
        
        deleted_nodes = len(nodes_to_delete)
        deleted_edges = 0
        
        if nodes_to_delete:
            placeholders = ",".join(["?"] * len(nodes_to_delete))
            # Delete connected edges
            c.execute(f"DELETE FROM kg_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})", nodes_to_delete + nodes_to_delete)
            deleted_edges = c.rowcount
            # Delete the nodes
            c.execute(f"DELETE FROM kg_nodes WHERE id IN ({placeholders})", nodes_to_delete)
            
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Entity already in exclusion list")
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
        
    return {"message": "Success", "deleted_nodes": deleted_nodes, "deleted_edges": deleted_edges}

@router.delete("/kg-exclusions/{exc_id}")
async def delete_kg_exclusion(exc_id: int, current_user: dict = Depends(auth.require_role("admin"))):
    """FR-30: Remove entity exclusion"""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("DELETE FROM kg_exclusions WHERE id = ?", (exc_id,))
    conn.commit()
    conn.close()
    return {"message": "Deleted successfully"}

# ── FR-31: System Monitoring (Insinyur TI) ──────────────────────────────────
import psutil
import shutil
import time
from datetime import datetime
