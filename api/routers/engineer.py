from fastapi import APIRouter, Depends, HTTPException
import sqlite3
import os
import psutil
from typing import Dict, Any
import auth

router = APIRouter(prefix="/api/engineer", tags=["engineer"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@router.get("/health")
async def get_system_health(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Fetch real-time system health metrics"""
    cpu_percent = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    # DB File sizes
    db_metadata_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    db_users_path = os.path.join(BASE_DIR, "data", "users.db")
    
    metadata_size = os.path.getsize(db_metadata_path) if os.path.exists(db_metadata_path) else 0
    users_size = os.path.getsize(db_users_path) if os.path.exists(db_users_path) else 0

    return {
        "cpu": cpu_percent,
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "percent": mem.percent
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "percent": disk.percent
        },
        "database": {
            "metadata_db_mb": round(metadata_size / (1024 * 1024), 2),
            "users_db_mb": round(users_size / (1024 * 1024), 2)
        },
        "uptime_seconds": int(time.time() - psutil.boot_time())
    }

@router.get("/queue")
async def get_processing_queue(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Mock processing queue based on recent audit logs"""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT user_id, action_type, resource_id, timestamp FROM audit_logs WHERE action_type IN ('UPLOAD_DOCUMENT', 'DELETE_DOCUMENT', 'REBUILD_GRAPH') ORDER BY timestamp DESC LIMIT 10")
    recent_tasks = [{"user": row[0], "action": row[1], "resource": row[2], "timestamp": row[3], "status": "COMPLETED"} for row in c.fetchall()]
    conn.close()
    
    return {"active_tasks": [], "recent_history": recent_tasks}

@router.get("/backups")
async def get_backups(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: List existing backups in the data directory"""
    data_dir = os.path.join(BASE_DIR, "data")
    backups = []
    if os.path.exists(data_dir):
        for f in os.listdir(data_dir):
            if f.endswith('.db') and 'backup' in f:
                path = os.path.join(data_dir, f)
                backups.append({
                    "filename": f,
                    "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
                    "created_at": datetime.fromtimestamp(os.path.getctime(path)).isoformat()
                })
    return {"backups": sorted(backups, key=lambda x: x['created_at'], reverse=True)}

@router.post("/backup")
async def create_backup(current_user: dict = Depends(auth.require_exact_role("insinyur ti"))):
    """FR-31: Manually trigger SQLite database backups"""
    import sqlite3
    data_dir = os.path.join(BASE_DIR, "data")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    dbs = ["legal_metadata.db", "users.db"]
    created_backups = []
    
    for db_name in dbs:
        src = os.path.join(data_dir, db_name)
        if os.path.exists(src):
            dst_name = db_name.replace('.db', f'_backup_{timestamp}.db')
            dst = os.path.join(data_dir, dst_name)
            
            # Use SQLite backup API for safe copy
            src_conn = sqlite3.connect(src)
            dst_conn = sqlite3.connect(dst)
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            
            created_backups.append(dst_name)
            
    return {"message": "Backup successful", "files": created_backups}

