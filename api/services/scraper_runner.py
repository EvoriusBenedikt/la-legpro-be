"""Process-isolated runner for authorized scraper jobs."""
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Callable

import run_all_scrapers

RUN_CMD = [sys.executable, "run_all_scrapers.py"]
RUN_TIMEOUT_S = 3600
MAX_OUTPUT = 4000

_state = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "started_by": None,
    "sources": [],
    "results": [],
    "error": None,
}
_lock = threading.Lock()


class AlreadyRunning(Exception):
    pass


class NoActiveSources(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _active_sources():
    inactive = run_all_scrapers._inactive_ids()
    return [module_name for _, module_name, _ in run_all_scrapers.SCRAPERS
            if module_name not in inactive]


def _parse_results(stdout):
    for line in reversed(stdout.splitlines()):
        if line.startswith("SCRAPE_RESULT_JSON: "):
            data = json.loads(line.removeprefix("SCRAPE_RESULT_JSON: "))
            return [
                {"id": item["id"], "label": item["label"], "status": item["status"],
                 "seconds": item["seconds"]}
                for item in data
            ]
    raise ValueError("runner did not return structured results")


def _worker(on_finished: Callable[[str], None] | None):
    try:
        try:
            completed = subprocess.run(
                RUN_CMD, cwd=run_all_scrapers.BASE_DIR, capture_output=True,
                text=True, timeout=RUN_TIMEOUT_S, check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"runner exited with status {completed.returncode}")
            results = _parse_results(completed.stdout[-MAX_OUTPUT:])
        except subprocess.TimeoutExpired:
            raise RuntimeError("scraper run timed out")
        except Exception as exc:
            with _lock:
                _state.update(state="failed", finished_at=_now(), error=str(exc)[:200])
            if on_finished:
                on_finished("failed")
            return
        failed_sources = any(item["status"].startswith("ERROR:") for item in results)
        final_state = "failed" if failed_sources else "completed"
        with _lock:
            _state.update(
                state=final_state,
                finished_at=_now(),
                results=results,
                error="One or more scraper sources failed" if failed_sources else None,
            )
        if on_finished:
            on_finished(final_state)
    finally:
        pass


def start(
    user: dict,
    on_finished: Callable[[str], None] | None = None,
    on_started: Callable[[], None] | None = None,
):
    sources = _active_sources()
    with _lock:
        if _state["state"] == "running":
            raise AlreadyRunning()
        if not sources:
            raise NoActiveSources()
        _state.update(
            state="running", started_at=_now(), finished_at=None,
            started_by=user.get("username"), sources=sources,
            results=[], error=None,
        )
        snapshot = dict(_state)
    if on_started:
        on_started()
    threading.Thread(target=_worker, args=(on_finished,), daemon=True).start()
    return snapshot


def snapshot():
    with _lock:
        return {key: (list(value) if isinstance(value, list) else value)
                for key, value in _state.items()}
