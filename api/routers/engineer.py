from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
import os
import psutil
import time
from datetime import datetime, timedelta
from typing import Optional
import auth
from services import backup_service, hardware_info, host_metrics, pg_service, task_queue

router = APIRouter(prefix="/api/engineer", tags=["engineer"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@router.get("/health")
async def get_system_health(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Fetch real-time system health metrics"""
    cpu_percent = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    # The data volume, not '/': inside a container '/' is the image overlay or
    # the desktop VM's virtual disk — a phantom volume that on the dev stack
    # hid the real, nearly full host drive behind a calm 7% (bug 2026-10-12).
    disk, disk_path = host_metrics.data_disk_usage()

    # Migration M3: sizes are PostgreSQL relation totals for the two legacy
    # table groups (same response keys the FE monitoring has always consumed).
    metadata_mb, users_mb = backup_service.group_sizes_mb()

    return {
        "cpu": cpu_percent,
        "memory": {"total": mem.total, "used": mem.used, "percent": mem.percent},
        "disk": {"total": disk.total, "used": disk.used, "percent": disk.percent, "path": disk_path},
        "database": {
            "metadata_db_mb": metadata_mb,
            "users_db_mb": users_mb
        },
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "runtime_scope": host_metrics.RUNTIME_SCOPE
    }


@router.get("/hardware")
async def get_hardware(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: full server hardware inspection — CPU model/cores/arch, GPU probe
    chain, kernel, runtime OS, hostname, plus the live memory split (total /
    used / available). The inventory is cached process-side (it cannot change
    mid-boot); the monitoring page fetches this once per session and on manual
    reload, never on the poll cadence."""
    return hardware_info.snapshot()


@router.get("/queue")
async def get_processing_queue(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Processing queue — every task (QUEUED/RUNNING/COMPLETED/FAILED/
    CANCELLED) with stage status, uploader, attempts and failure reason, plus
    per-status counters. The FE filters/paginates client-side (<= 200 tasks)."""
    return task_queue.snapshot()


@router.post("/queue/{task_id}/cancel")
async def cancel_queue_task(task_id: str, current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """Cancel a queued task at once, or flag a running one to stop at its next
    stage boundary (a model call already in flight cannot be interrupted)."""
    result = task_queue.request_cancel(task_id)
    if result == "invalid":
        raise HTTPException(status_code=409, detail="Tugas tidak dapat dibatalkan pada status ini.")
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "CANCEL_TASK", task_id, f"Membatalkan tugas pemrosesan ({result})")
    return {"result": result}


@router.post("/queue/{task_id}/retry")
async def retry_queue_task(task_id: str, background_tasks: BackgroundTasks,
                           current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """Re-run a FAILED or CANCELLED task from its first stage."""
    try:
        kind, args = task_queue.prepare_manual_retry(task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    from routers import repository
    fn = repository.process_document_background if kind == "parse" \
        else repository.ingest_document_background
    background_tasks.add_task(fn, *args)
    from services.db_service import log_audit
    log_audit(current_user.get("id", ""), "RETRY_TASK", task_id, "Mengulang tugas pemrosesan secara manual")
    return {"result": "queued"}


@router.get("/backups")
async def get_backups(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: List existing backups in the data directory"""
    # Migration M3: lists the PG tar snapshots (legacy *_backup_*.db files
    # stay on disk but are no longer produced or restorable here).
    return {"backups": backup_service.list_backups()}


@router.post("/backup")
async def create_backup(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Manually trigger a PostgreSQL snapshot backup"""
    name = backup_service.create_backup()
    return {"message": "Backup successful", "files": [name]}


# ── FR-31 HIGH PRIORITY: Active Sessions ─────────────────────────────────────

@router.get("/active-sessions")
async def get_active_sessions(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Return users who have been active in the last 5 minutes."""
    sessions = pg_service.query(
        "SELECT username, role, "
        "to_char(last_seen, 'YYYY-MM-DD HH24:MI:SS') AS last_seen, "
        "COALESCE(ip_address, 'N/A') AS ip_address "
        "FROM active_sessions WHERE last_seen >= NOW() - INTERVAL '5 minutes' "
        "ORDER BY last_seen DESC"
    )
    return {"active_sessions": sessions, "count": len(sessions)}


# ── FR-31 HIGH PRIORITY: API Endpoint Health ─────────────────────────────────

@router.get("/api-stats")
async def get_api_stats(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Return in-memory per-route request counts and error rates."""
    try:
        from services.app_state import API_STATS
    except ImportError:
        API_STATS = {}

    results = []
    for route, counts in API_STATS.items():
        total = counts.get("total", 0)
        errors = counts.get("errors", 0)
        error_rate = round((errors / total * 100), 1) if total > 0 else 0.0
        results.append({
            "route": route,
            "total_requests": total,
            "error_count": errors,
            "error_rate_pct": error_rate
        })
    # Sort by total requests descending, skip noisy internal routes
    results = [r for r in results if "api-stats" not in r["route"] and "/health" not in r["route"]]
    results.sort(key=lambda x: x["total_requests"], reverse=True)
    return {"stats": results[:20]}


# ── FR-31 HIGH PRIORITY: Audit Log Viewer ────────────────────────────────────

@router.get("/audit-logs")
async def get_audit_logs(
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(auth.require_exact_role("insinyur ti"))
):
    """FR-31: Fetch full audit logs with filtering."""
    where_sql = "WHERE 1=1"
    params = []

    if search:
        where_sql += " AND (user_id LIKE %s OR resource_id LIKE %s OR details LIKE %s)"
        like = f"%{search}%"
        params.extend([like, like, like])
    if action and action != "Semua":
        where_sql += " AND action_type = %s"
        params.append(action)

    total_row = pg_service.query_one(
        f"SELECT COUNT(*) AS n FROM audit_logs {where_sql}", params)
    total = total_row["n"] if total_row else 0

    # id DESC == chronological DESC (identity follows insertion order, and
    # timestamp is set on insert); avoids the text/timestamptz alias clash.
    logs = pg_service.query(
        "SELECT id, to_char(timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp, "
        "user_id, action_type, resource_id, details "
        f"FROM audit_logs {where_sql} ORDER BY id DESC LIMIT %s OFFSET %s",
        params + [limit, offset]
    )

    action_types = [r["action_type"] for r in pg_service.query(
        "SELECT DISTINCT action_type FROM audit_logs ORDER BY action_type")]

    return {"logs": logs, "total": total, "action_types": action_types}

# ── FR-31 MEDIUM PRIORITY: Metrics & Backup Schedule ─────────────────────────

@router.get("/metrics-history")
async def get_metrics_history(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """Fetch the last 48 hours of system metrics (Time Series data)."""
    # We poll every 1 minute. 48 hours = 2880 minutes.
    rows = pg_service.query(
        "SELECT to_char(timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp, "
        "cpu, ram, disk, metadata_db_mb, users_db_mb "
        "FROM system_metrics ORDER BY id DESC LIMIT 2880"
    )
    rows.reverse()
    return {"metrics": rows}

@router.get("/backup-config")
async def get_backup_config(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    config_path = os.path.join(BASE_DIR, "data", "backup_config.json")
    if os.path.exists(config_path):
        import json
        with open(config_path, "r") as f:
            return json.load(f)
    return {"frequency": "daily", "time": "02:00", "retention_count": 5}

from pydantic import BaseModel
class BackupConfig(BaseModel):
    frequency: str
    time: str
    retention_count: int

@router.post("/backup-config")
async def set_backup_config(config: BackupConfig, current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    config_path = os.path.join(BASE_DIR, "data", "backup_config.json")
    import json
    with open(config_path, "w") as f:
        json.dump(config.model_dump(), f)
    return {"message": "Backup configuration saved successfully", "config": config.model_dump()}

# ── FR-31 NICE-TO-HAVE: Engineering Analytics ────────────────────────────────

@router.get("/user-stats")
async def get_user_activity_stats(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    try:
        rows = pg_service.query('''
            SELECT user_id, action_type, COUNT(*) as count 
            FROM audit_logs 
            GROUP BY user_id, action_type
        ''')
        
        stats = {}
        for r in rows:
            uid = r["user_id"]
            action = r["action_type"]
            count = r["count"]
            if uid not in stats:
                stats[uid] = {"user_id": uid, "total_actions": 0, "breakdown": {}}
            stats[uid]["breakdown"][action] = count
            stats[uid]["total_actions"] += count
            
        return {"stats": list(stats.values())}
    except Exception as e:
        return {"stats": [], "error": str(e)}

@router.get("/llm-metrics")
async def get_llm_metrics(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    try:
        agg_row = pg_service.query_one('''
            SELECT 
                COUNT(*) as total_calls,
                SUM(tokens_used) as total_tokens,
                AVG(latency_ms)::float as avg_latency_ms,
                SUM(cost_estimate)::float as total_cost
            FROM llm_metrics
        ''')
        agg = dict(agg_row or {})
        
        recent = pg_service.query(
            "SELECT endpoint, tokens_used, latency_ms, cost_estimate, "
            "to_char(timestamp, 'YYYY-MM-DD HH24:MI:SS') AS timestamp "
            "FROM llm_metrics ORDER BY id DESC LIMIT 50")
        
        return {"aggregate": agg, "recent": recent}
    except Exception as e:
        return {"aggregate": {}, "recent": [], "error": str(e)}

@router.get("/kg-history")
async def get_kg_rebuild_history(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    try:
        history = pg_service.query(
            "SELECT id, to_char(start_time, 'YYYY-MM-DD HH24:MI:SS') AS start_time, "
            "to_char(end_time, 'YYYY-MM-DD HH24:MI:SS') AS end_time, "
            "duration_s, nodes_changed, edges_changed, status "
            "FROM kg_rebuild_history ORDER BY id DESC LIMIT 20")
        return {"history": history}
    except Exception as e:
        return {"history": [], "error": str(e)}

@router.get("/error-rates")
async def get_error_rates(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    try:
        from services.app_state import API_STATS
    except ImportError:
        API_STATS = {}
        
    errors = []
    for route, counts in API_STATS.items():
        if counts.get("errors", 0) > 0:
            errors.append({
                "route": route,
                "total_requests": counts.get("total", 0),
                "error_count": counts.get("errors", 0),
                "error_rate_pct": round((counts.get("errors", 0) / counts.get("total", 1) * 100), 1),
                "last_error_time": counts.get("last_error_time")
            })
            
    errors.sort(key=lambda x: x["error_count"], reverse=True)
    return {"errors": errors}
