"""Shared in-memory application state (single module instance).

These dicts used to live in api/main.py. Under `uvicorn api.main:app`, any
`from main import ...` inside a router loaded main.py a SECOND time under the
module name "main" — creating separate dict objects, so the stats middleware
(writing to api.main's dicts) and engineer.py (reading main's dicts) never saw
each other's data. Living in their own module, every importer shares exactly
one instance.

Created during the Phase 2 refactor follow-up (contents unchanged).
"""

# ── In-memory API stats for monitoring (FR-31) ──────────────────────────────
API_STATS: dict = {}  # {route: {"total": int, "errors": int}}
ACTIVE_TASKS: dict = {}  # {task_id: {"name": str, "status": str, "start_time": datetime, "end_time": Optional[datetime]}}
