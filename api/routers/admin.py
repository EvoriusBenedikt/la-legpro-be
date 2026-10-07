from fastapi import APIRouter, Depends, HTTPException, Query, Response
import csv
import io
import json
import os
from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from psycopg import errors as pg_errors
import auth
from services import pg_service
from services.db_service import log_audit

router = APIRouter(prefix="/api/admin", tags=["admin"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class KGExclusion(BaseModel):
    entity_name: str

# Per-status cap for the dashboard's doc_details payload: doc_status keeps
# the true totals while each detail list ships at most this many rows. The
# uncapped version serialized the whole corpus (952+ docs, ~1 MB JSON) for
# four 200px scrollable card lists.
DOC_DETAILS_LIMIT = 100

@router.get("/dashboard")
async def admin_dashboard(current_user: dict = Depends(auth.require_role("admin"))):
    """FR-26: Admin-only dashboard with system stats and audit logs."""
    # 1. Document Processing Status & Details (ORDER BY id keeps the
    #    DOC_DETAILS_LIMIT slice deterministic)
    all_docs = pg_service.query("SELECT id, judul, status, nomor FROM regulations ORDER BY id")
    
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

    # Counts above are computed first, then the payload is bounded; the FE
    # shows "Menampilkan N pertama dari M dokumen" when value > len(list).
    doc_details = {k: v[:DOC_DETAILS_LIMIT] for k, v in doc_details.items()}

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

    # 5. (Moved 2026-10-06, critique P1) Audit logs no longer ride this
    #    payload: the LIMIT-100 snapshot could not be paged past the newest
    #    100 rows, date-filtered, or exported. See GET /api/admin/audit-logs
    #    (+ /export) below — the FE audit viewer fetches it directly.

    # 6. System Health (Migration M3: the vector store is the PG chunks
    # table now; the "sqlite"/"chromadb" key names are kept because the
    # frozen FE reads them — both probe PostgreSQL. "sqlite" was hardcoded
    # True until 2026-09-30, which made the DB tile a placebo.)
    db_ok = False
    try:
        pg_service.query_one("SELECT 1")
        db_ok = True
    except Exception as e:
        print(f"Database health check failed: {e}")

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
        "system_health": {
            "sqlite": db_ok,
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

# ── KG-exclusion blast-radius preview (2026-10-06, critique P2) ────────────

@router.get("/kg-exclusions/preview")
async def preview_kg_exclusion(entity_name: str = Query(..., min_length=1),
                               current_user: dict = Depends(auth.require_role("admin"))):
    """Dry run of POST /kg-exclusions: the same case-insensitive label match
    the delete uses, counted instead of executed, so the FE confirm dialog
    can state exactly what will be destroyed before the admin commits."""
    name = entity_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Entity name is required")
    node_ids = [r["id"] for r in pg_service.query(
        "SELECT id FROM kg_nodes WHERE LOWER(label) = LOWER(%s)", (name,))]
    matching_edges = 0
    if node_ids:
        edge_row = pg_service.query_one(
            "SELECT COUNT(*) AS n FROM kg_edges "
            "WHERE source_id = ANY(%s) OR target_id = ANY(%s)",
            (node_ids, node_ids))
        matching_edges = edge_row["n"] if edge_row else 0
    already_excluded = pg_service.query_one(
        "SELECT 1 FROM kg_exclusions WHERE entity_name = %s", (name,)) is not None
    return {
        "entity_name": name,
        "matching_nodes": len(node_ids),
        "matching_edges": matching_edges,
        "already_excluded": already_excluded,
    }

# ── User management (2026-10-06, critique P0) ──────────────────────────────
# PRODUCT.md defines the admin role as "User & role management", but no UI or
# API existed. Access is exact-role admin (user decision): sekretaris
# perusahaan also reaches /api/admin/* via require_role("admin"), but user
# management is deliberately stricter. Actor-relative rule: only accounts
# STRICTLY BELOW the actor's role level can be modified, so an admin (4)
# manages pengguna/manajer/direktur and can never touch another admin,
# sekretaris, insinyur ti, or their own account.

class RoleUpdate(BaseModel):
    role: str

class StatusUpdate(BaseModel):
    is_active: bool

def _user_payload(row: dict, actor: dict) -> dict:
    actor_level = auth.get_role_level(actor.get("role", "pengguna"))
    row_level = auth.get_role_level(row["role"] or "pengguna")
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
        # The FE renders row controls from these flags; every mutation
        # re-checks server-side (_managed_target) — flags are UX, not
        # authorization.
        "manageable": row_level < actor_level and row["id"] != actor["id"],
        "is_self": row["id"] == actor["id"],
    }

@router.get("/users")
async def admin_list_users(current_user: dict = Depends(auth.require_exact_role("admin"))):
    """All accounts with actor-relative manageability flags. Search/role
    filtering is FE-side: a small corporate directory in one fetch keeps the
    panel instant instead of round-tripping per keystroke."""
    rows = pg_service.query(
        "SELECT id, username, email, role, is_active, "
        "to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at "
        "FROM users ORDER BY LOWER(username)")
    return {"users": [_user_payload(r, current_user) for r in rows]}

def _managed_target(user_id: str, current_user: dict) -> dict:
    """Shared guard chain for the role/status mutations: target exists, is
    not the actor, and sits strictly below the actor's role level."""
    target = pg_service.query_one(
        "SELECT id, username, role, is_active FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == current_user["id"]:
        raise HTTPException(status_code=400, detail="You cannot modify your own account from this panel")
    actor_level = auth.get_role_level(current_user.get("role", "pengguna"))
    if auth.get_role_level(target["role"] or "pengguna") >= actor_level:
        raise HTTPException(
            status_code=403,
            detail="Only accounts below your own role level can be managed")
    return target

@router.patch("/users/{user_id}/role")
async def admin_change_role(user_id: str, req: RoleUpdate,
                            current_user: dict = Depends(auth.require_exact_role("admin"))):
    """Promote/demote within the sub-actor slice of the ladder. Takes effect
    on the target's NEXT request: get_current_user re-reads role from the DB
    (auth.py), so the stale 24 h JWT claim cannot outlive the change."""
    new_role = req.role.strip().lower()
    if new_role not in auth.ROLE_LEVELS:
        raise HTTPException(status_code=400, detail="Unknown role")
    actor_level = auth.get_role_level(current_user.get("role", "pengguna"))
    if auth.get_role_level(new_role) >= actor_level:
        raise HTTPException(status_code=403, detail="The new role must be below your own role level")
    target = _managed_target(user_id, current_user)
    old_role = (target["role"] or "pengguna").lower()
    if old_role == new_role:
        raise HTTPException(status_code=400, detail="User already has this role")
    pg_service.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
    log_audit(current_user["id"], "CHANGE_ROLE", user_id,
              f"{target['username']}: {old_role} -> {new_role}")
    return {"message": "Role updated", "id": user_id, "role": new_role}

@router.patch("/users/{user_id}/status")
async def admin_set_status(user_id: str, req: StatusUpdate,
                           current_user: dict = Depends(auth.require_exact_role("admin"))):
    """Deactivate/reactivate. Deactivation bites on the target's next request
    (get_current_user checks is_active per request) — no waiting for JWT
    expiry — and drops their active_sessions row."""
    target = _managed_target(user_id, current_user)
    if bool(target["is_active"]) == req.is_active:
        raise HTTPException(status_code=400, detail="Account already in this state")
    pg_service.execute("UPDATE users SET is_active = %s WHERE id = %s",
                       (req.is_active, user_id))
    if not req.is_active:
        pg_service.execute("DELETE FROM active_sessions WHERE user_id = %s", (user_id,))
    log_audit(current_user["id"],
              "ACTIVATE_USER" if req.is_active else "DEACTIVATE_USER",
              user_id, target["username"])
    return {"message": "Status updated", "id": user_id, "is_active": req.is_active}

# ── Audit viewer (2026-10-06, critique P1) ─────────────────────────────────
# Server-side pagination, date range, username join, and export replace the
# FE's client-side slicing of the dashboard's old 100-row snapshot. Grammar
# mirrors the engineer-only GET /api/engineer/audit-logs. Access stays at
# require_role("admin") — sekretaris perusahaan could already see audit rows
# via the dashboard payload; only user management is exact-role admin.

AUDIT_FROM_JOIN = "FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id "
AUDIT_SELECT = (
    "SELECT a.id, to_char(a.timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp, "
    "a.user_id, u.username, a.action_type, a.resource_id, a.details ")
EXPORT_COLUMNS = ["id", "timestamp", "user_id", "username",
                  "action_type", "resource_id", "details"]

def _audit_filters(search: Optional[str], action: Optional[str],
                   date_from: Optional[str], date_to: Optional[str]):
    where_sql = "WHERE 1=1"
    params: list = []
    if search:
        where_sql += (" AND (a.user_id LIKE %s OR u.username LIKE %s "
                      "OR a.resource_id LIKE %s OR a.details LIKE %s)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if action and action != "Semua":
        where_sql += " AND a.action_type = %s"
        params.append(action)
    # date_to is inclusive (< date + 1 day). Values are validated as
    # YYYY-MM-DD before reaching PG so bad input is a 400, not a 500.
    for value, clause in ((date_from, " AND a.timestamp >= %s::date"),
                          (date_to, " AND a.timestamp < (%s::date + INTERVAL '1 day')")):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail="Dates must use the YYYY-MM-DD format")
            where_sql += clause
            params.append(value)
    return where_sql, params

@router.get("/audit-logs")
async def admin_audit_logs(
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(auth.require_role("admin")),
):
    """Paginated audit feed with username join (rows keep user_id; username
    is NULL for accounts that no longer exist — the FE falls back to the
    UUID)."""
    where_sql, params = _audit_filters(search, action, date_from, date_to)
    total_row = pg_service.query_one(
        f"SELECT COUNT(*) AS n {AUDIT_FROM_JOIN}{where_sql}", params)
    total = total_row["n"] if total_row else 0
    # id DESC == chronological DESC (identity follows insertion order) —
    # same ordering note as engineer.get_audit_logs.
    logs = pg_service.query(
        f"{AUDIT_SELECT}{AUDIT_FROM_JOIN}{where_sql} ORDER BY a.id DESC LIMIT %s OFFSET %s",
        params + [page_size, (page - 1) * page_size])
    action_types = [r["action_type"] for r in pg_service.query(
        "SELECT DISTINCT action_type FROM audit_logs "
        "WHERE action_type IS NOT NULL ORDER BY action_type")]
    return {"logs": logs, "total": total, "page": page,
            "page_size": page_size, "action_types": action_types}

@router.get("/audit-logs/export")
async def admin_audit_export(
    fmt: str = Query("csv", alias="format"),
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    current_user: dict = Depends(auth.require_role("admin")),
):
    """Download the FULL filtered set (not just the visible page) as CSV or
    JSON. Bounded at 100k rows — an unfiltered dump is the whole table, and
    the cap keeps memory sane on any realistic corpus."""
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")
    where_sql, params = _audit_filters(search, action, date_from, date_to)
    rows = pg_service.query(
        f"{AUDIT_SELECT}{AUDIT_FROM_JOIN}{where_sql} ORDER BY a.id DESC LIMIT 100000",
        params)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    disposition = f'attachment; filename="audit-logs-{stamp}.{fmt}"'
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_COLUMNS)
        for r in rows:
            writer.writerow(["" if r.get(c) is None else r.get(c) for c in EXPORT_COLUMNS])
        return Response(content=buf.getvalue(),
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": disposition})
    return Response(content=json.dumps(rows, ensure_ascii=False, indent=2),
                    media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": disposition})
