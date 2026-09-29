"""Asyncio background loops for system metrics and scheduled DB backups.

Moved from api/main.py during the Phase 2 refactor.
Deliberately NOT merged with services/alert_scheduler.py: that module is an
APScheduler cron for contract-expiration email alerts, while these are
per-minute asyncio loops — no overlap. Both are still started from main.py's
startup events.

Migration M3 (cutover): metrics and backups now target PostgreSQL. DB sizes
come from relation sizes of the two legacy table groups (see
services.backup_service), and the nightly backup is a COPY-based tar
snapshot instead of sqlite3 file copies.
"""
import os
import json
import asyncio
import psutil
from datetime import datetime

from services import backup_service, pg_service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def system_metrics_loop():
    # Initial pause to let startup finish
    await asyncio.sleep(10)
    while True:
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            metadata_size, users_size = backup_service.group_sizes_mb()

            with pg_service.get_conn() as conn:
                conn.execute(
                    "INSERT INTO system_metrics (cpu, ram, disk, metadata_db_mb, users_db_mb) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (cpu, mem.percent, disk.percent, metadata_size, users_size))

                # Keep only last 2880 records (48 hours at 1 minute intervals)
                conn.execute(
                    "DELETE FROM system_metrics WHERE id NOT IN "
                    "(SELECT id FROM system_metrics ORDER BY id DESC LIMIT 2880)")
        except Exception as e:
            print(f"Metrics loop error: {e}")

        await asyncio.sleep(60)

async def backup_scheduler_loop():
    await asyncio.sleep(20)
    while True:
        try:
            config_path = os.path.join(BASE_DIR, "data", "backup_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)

                # Format: {"frequency": "daily", "time": "02:00", "retention_count": 5}
                now = datetime.now()
                target_time = config.get("time", "02:00")
                if now.strftime("%H:%M") == target_time:
                    # check if we already backed up today
                    today = now.strftime("%Y%m%d")
                    if not backup_service.has_backup_from(today):
                        backup_service.create_backup(timestamp=now.strftime("%Y%m%d_%H%M%S"))

                        # Delete older backups exceeding retention.
                        # Note: one archive per run now (legacy runs produced
                        # 2 sqlite files per run), so retention_count is the
                        # number of archives kept.
                        backup_service.prune_backups(int(config.get("retention_count", 5)))

        except Exception as e:
            print(f"Backup loop error: {e}")

        await asyncio.sleep(60)

def start_background_loops():
    """Create the asyncio background tasks. Called from main.py's async startup event."""
    asyncio.create_task(system_metrics_loop())
    asyncio.create_task(backup_scheduler_loop())
