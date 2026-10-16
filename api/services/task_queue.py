"""Document-pipeline task registry (FR-31 "Antrean Pemrosesan").

Wraps the in-memory ACTIVE_TASKS dict with a richer lifecycle so System
Monitoring can show a truthful queue:

    QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELLED

Each task carries its document id, uploader, per-stage status, attempt count,
failure reason and (when applicable) the next auto-retry time. State is
process-local and lost on restart, exactly like ACTIVE_TASKS always was.

Cancellation is cooperative: a QUEUED task is cancelled at once; a RUNNING
task is flagged and aborts at the next stage boundary (`begin_stage`), because
a model call already in flight cannot be interrupted.
"""
import threading
from datetime import datetime, timedelta
from typing import Optional

from services import pg_service
from services.app_state import ACTIVE_TASKS

MAX_ATTEMPTS = 3          # initial run + 2 automatic retries
AUTO_RETRY_DELAY_S = 30
MAX_TASKS = 200           # finished tasks beyond this are pruned oldest-first

STAGES = {
    "parse": ["Ekstraksi teks", "Klasifikasi AI"],
    "ingest": ["Ekstraksi teks", "Deteksi duplikat", "Ringkasan konteks",
               "Chunking & embedding", "Graf pengetahuan"],
}

_lock = threading.RLock()
_timers: dict = {}


class TaskCancelled(Exception):
    """Raised at a stage boundary when the operator cancelled the task."""


def task_id_for(doc_id) -> str:
    return f"task_{doc_id}"


def _lookup_uploader(doc_id) -> Optional[str]:
    try:
        row = pg_service.query_one(
            "SELECT uploaded_by_name FROM upload_history WHERE doc_id = %s", (str(doc_id),))
        return (row or {}).get("uploaded_by_name") or None
    except Exception:
        return None


def _prune():
    finished = [(k, v) for k, v in ACTIVE_TASKS.items()
                if v.get("status") in ("COMPLETED", "FAILED", "CANCELLED")]
    excess = len(ACTIVE_TASKS) - MAX_TASKS
    if excess <= 0:
        return
    finished.sort(key=lambda kv: kv[1].get("end_time") or datetime.min)
    for k, _ in finished[:excess]:
        ACTIVE_TASKS.pop(k, None)


def _build(doc_id, kind, name, filename, uploader, status, retry_args, prev, attempts):
    now = datetime.now()
    return {
        "name": name,
        "kind": kind,
        "doc_id": str(doc_id),
        "filename": filename,
        "uploader": uploader or prev.get("uploader") or _lookup_uploader(doc_id),
        "status": status,
        "stages": [{"name": n, "status": "pending"} for n in STAGES[kind]],
        "queued_at": now if status == "QUEUED" else (prev.get("queued_at") or now),
        "start_time": now if status == "RUNNING" else None,
        "end_time": None,
        "attempts": attempts,
        "max_attempts": MAX_ATTEMPTS,
        "error": None,
        "retryable": True,
        "cancel_requested": False,
        "next_retry_at": None,
        "retry_args": retry_args,
    }


def enqueue(doc_id, kind, name, filename, retry_args, uploader=None):
    """Register a task as QUEUED (before handing it to BackgroundTasks)."""
    tid = task_id_for(doc_id)
    with _lock:
        prev = ACTIVE_TASKS.get(tid) or {}
        ACTIVE_TASKS[tid] = _build(doc_id, kind, name, filename, uploader, "QUEUED",
                                   retry_args, prev, prev.get("attempts", 0))
        _prune()
    return tid


def start(doc_id, kind, name, filename, retry_args, chained=False) -> Optional[str]:
    """Move a task to RUNNING. Returns None when it was cancelled while queued
    (the worker must then return without doing any work)."""
    tid = task_id_for(doc_id)
    with _lock:
        prev = ACTIVE_TASKS.get(tid) or {}
        if prev.get("status") == "CANCELLED":
            return None
        # A chained start continues the same pipeline run (process -> ingest,
        # or confirm -> ingest), so it must not count as another attempt.
        attempts = prev.get("attempts", 0) + (0 if (chained and prev) else 1)
        ACTIVE_TASKS[tid] = _build(doc_id, kind, name, filename, None, "RUNNING",
                                   retry_args, prev, attempts)
        _prune()
    return tid


def begin_stage(tid, index: int):
    """Mark stage `index` running (earlier ones done); abort if cancelled."""
    with _lock:
        task = ACTIVE_TASKS.get(tid)
        if not task:
            return
        if task.get("cancel_requested"):
            raise TaskCancelled()
        for i, st in enumerate(task["stages"]):
            if i < index and st["status"] in ("pending", "running"):
                st["status"] = "done"
            elif i == index:
                st["status"] = "running"


def complete(tid):
    with _lock:
        task = ACTIVE_TASKS.get(tid)
        if not task:
            return
        for st in task["stages"]:
            if st["status"] in ("pending", "running"):
                st["status"] = "done"
        task["status"] = "COMPLETED"
        task["end_time"] = datetime.now()


def fail(tid, error, retryable=True):
    with _lock:
        task = ACTIVE_TASKS.get(tid)
        if not task:
            return
        for st in task["stages"]:
            if st["status"] == "running":
                st["status"] = "failed"
        task["status"] = "FAILED"
        task["end_time"] = datetime.now()
        task["error"] = str(error)[:500]
        task["retryable"] = retryable
        if retryable and task["attempts"] < task["max_attempts"]:
            _schedule_auto_retry(tid, task)


def cancelled(tid):
    with _lock:
        task = ACTIVE_TASKS.get(tid)
        if not task:
            return
        for st in task["stages"]:
            if st["status"] == "running":
                st["status"] = "failed"
        task["status"] = "CANCELLED"
        task["end_time"] = datetime.now()
        task["error"] = "Dibatalkan oleh operator."
        task["next_retry_at"] = None


def _schedule_auto_retry(tid, task):
    task["next_retry_at"] = datetime.now() + timedelta(seconds=AUTO_RETRY_DELAY_S)
    timer = threading.Timer(AUTO_RETRY_DELAY_S, _run_retry, args=(tid,))
    timer.daemon = True
    _timers[tid] = timer
    timer.start()


def _run_retry(tid):
    with _lock:
        _timers.pop(tid, None)
        task = ACTIVE_TASKS.get(tid)
        if not task or task["status"] != "FAILED" or not task.get("retry_args"):
            return
        kind, args = task["kind"], task["retry_args"]
    _dispatch(kind, args)


def _dispatch(kind, args):
    from routers import repository  # lazy: repository imports this module
    fn = repository.process_document_background if kind == "parse" \
        else repository.ingest_document_background
    fn(*args)


def request_cancel(tid) -> str:
    """Cancel a task. Returns 'cancelled' (immediate), 'requested' (will abort
    at the next stage boundary), or 'invalid'."""
    with _lock:
        task = ACTIVE_TASKS.get(tid)
        if not task:
            return "invalid"
        status = task["status"]
        timer = _timers.pop(tid, None)
        if timer:
            timer.cancel()
        if status == "QUEUED" or (status == "FAILED" and task.get("next_retry_at")):
            task["status"] = "CANCELLED"
            task["end_time"] = datetime.now()
            task["error"] = "Dibatalkan oleh operator."
            task["next_retry_at"] = None
            return "cancelled"
        if status == "RUNNING":
            task["cancel_requested"] = True
            return "requested"
        return "invalid"


def prepare_manual_retry(tid):
    """Validate + flip a FAILED/CANCELLED task back to QUEUED. Returns
    (kind, retry_args) for the caller to schedule, or raises ValueError."""
    with _lock:
        task = ACTIVE_TASKS.get(tid)
        if not task:
            raise ValueError("Tugas tidak ditemukan.")
        if task["status"] not in ("FAILED", "CANCELLED"):
            raise ValueError("Hanya tugas gagal atau dibatalkan yang dapat diulang.")
        if not task.get("retryable") or not task.get("retry_args"):
            raise ValueError("Tugas ini tidak dapat diulang (berkas sumber sudah dihapus).")
        timer = _timers.pop(tid, None)
        if timer:
            timer.cancel()
        kind, args = task["kind"], task["retry_args"]
        ACTIVE_TASKS[tid] = _build(task["doc_id"], kind, task["name"], task["filename"],
                                   task.get("uploader"), "QUEUED", args, task,
                                   task.get("attempts", 0))
        return kind, args


def snapshot() -> dict:
    def iso(d):
        return d.isoformat() if d else None

    with _lock:
        tasks = []
        for tid, t in ACTIVE_TASKS.items():
            stages = t.get("stages") or []
            current = next((s["name"] for s in stages if s["status"] == "running"), None)
            done = sum(1 for s in stages if s["status"] == "done")
            tasks.append({
                "id": tid,
                "name": t.get("name"),
                "doc_id": t.get("doc_id"),
                "filename": t.get("filename"),
                "uploader": t.get("uploader"),
                "status": t["status"],
                "stage": current,
                "stages": stages,
                "progress": (done / len(stages)) if stages else None,
                "queued_at": iso(t.get("queued_at")),
                "start_time": iso(t.get("start_time")),
                "end_time": iso(t.get("end_time")),
                "attempts": t.get("attempts", 0),
                "max_attempts": t.get("max_attempts", MAX_ATTEMPTS),
                "error": t.get("error"),
                "retryable": bool(t.get("retryable", True) and t.get("retry_args")),
                "cancel_requested": bool(t.get("cancel_requested")),
                "next_retry_at": iso(t.get("next_retry_at")),
            })
    tasks.sort(key=lambda x: x["end_time"] or x["start_time"] or x["queued_at"] or "", reverse=True)
    counts = {k: 0 for k in ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED")}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {"tasks": tasks, "counts": counts}
