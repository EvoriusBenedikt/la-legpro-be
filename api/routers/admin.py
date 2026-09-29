from fastapi import APIRouter, Depends, HTTPException
import os
from pydantic import BaseModel
from psycopg import errors as pg_errors
import auth
from services import pg_service

router = APIRouter(prefix="/api/admin", tags=["admin"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class KGExclusion(BaseModel):
    entity_name: str

@router.get("/dashboard")
async def admin_dashboard(current_user: dict = Depends(auth.require_role("admin"))):
    """FR-26: Admin-only dashboard with system stats and audit logs."""
    # 1. Document Processing Status & Details
    all_docs = pg_service.query("SELECT id, judul, status, nomor FROM regulations")
    
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
    klas_rows = pg_service.query(
        "SELECT klasifikasi, COUNT(*) as count FROM regulations "
        "WHERE klasifikasi IS NOT NULL GROUP BY klasifikasi")
    doc_by_klasifikasi = {r["klasifikasi"]: r["count"] for r in klas_rows}

    # 3. Document Volume by Jenis
    doc_by_jenis = pg_service.query(
        "SELECT jenis, COUNT(*) as count FROM regulations GROUP BY jenis ORDER BY count DESC LIMIT 10")

    # 4. Active Access Grants count (Migration M3: expires_at is TIMESTAMPTZ
    # in PG -- the legacy empty-string case no longer exists)
    grants_row = pg_service.query_one(
        "SELECT COUNT(*) AS n FROM access_grants "
        "WHERE expires_at IS NULL OR expires_at >= NOW()")
    active_grants = grants_row["n"] if grants_row else 0

    # 5. Recent Audit Logs (last 100)
    audit_logs = pg_service.query(
        "SELECT id, to_char(timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp, "
        "user_id, action_type, resource_id, details "
        "FROM audit_logs ORDER BY id DESC LIMIT 100")

    # 6. System Health (Migration M3: the vector store is the PG chunks
    # table now; the "chromadb" key is kept because the frozen FE reads it)
    chroma_ok = False
    try:
        pg_service.query_one("SELECT 1 FROM chunks LIMIT 1")
        chroma_ok = True
    except Exception as e:
        print(f"Vector store health check failed: {e}")

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
    rows = pg_service.query(
        "SELECT id, entity_name, to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM kg_exclusions ORDER BY created_at DESC")
    exclusions = [{"id": r["id"], "entity_name": r["entity_name"], "created_at": r["created_at"]} for r in rows]
    return {"exclusions": exclusions}

@router.post("/kg-exclusions")
async def add_kg_exclusion(req: KGExclusion, current_user: dict = Depends(auth.require_role("admin"))):
    """FR-30: Add entity exclusion and optionally delete existing nodes"""
    
    entity_name = req.entity_name.strip()
    if not entity_name:
        raise HTTPException(status_code=400, detail="Entity name is required")
        
    try:
        # Single transaction: exclusion insert + node/edge cleanup commit together.
        with pg_service.get_conn() as conn:
            # Add to exclusion list
            conn.execute("INSERT INTO kg_exclusions (entity_name) VALUES (%s)", (entity_name,))

            # Auto-cleanup: Delete any existing nodes with this exact label (case-insensitive)
            cur = conn.execute("SELECT id FROM kg_nodes WHERE LOWER(label) = LOWER(%s)", (entity_name,))
            nodes_to_delete = [row["id"] for row in cur.fetchall()]

            deleted_nodes = len(nodes_to_delete)
            deleted_edges = 0

            if nodes_to_delete:
                # Delete connected edges (ANY(%s) replaces placeholder building)
                cur = conn.execute(
                    "DELETE FROM kg_edges WHERE source_id = ANY(%s) OR target_id = ANY(%s)",
                    (nodes_to_delete, nodes_to_delete))
                deleted_edges = cur.rowcount
                # Delete the nodes
                conn.execute("DELETE FROM kg_nodes WHERE id = ANY(%s)", (nodes_to_delete,))
    except pg_errors.UniqueViolation:
        raise HTTPException(status_code=400, detail="Entity already in exclusion list")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    return {"message": "Success", "deleted_nodes": deleted_nodes, "deleted_edges": deleted_edges}

@router.delete("/kg-exclusions/{exc_id}")
async def delete_kg_exclusion(exc_id: int, current_user: dict = Depends(auth.require_role("admin"))):
    """FR-30: Remove entity exclusion"""
    pg_service.execute("DELETE FROM kg_exclusions WHERE id = %s", (exc_id,))
    return {"message": "Deleted successfully"}
