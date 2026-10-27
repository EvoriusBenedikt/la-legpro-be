import json
import sys
import time

import pytest

pytestmark = pytest.mark.db

from run_all_scrapers import SCRAPERS


def auth_for(make_user, mint_token, role):
    return {"Authorization": f"Bearer {mint_token(make_user(role, role=role))}"}


def test_scrape_run_authorization_and_happy_path(client, make_user, mint_token, monkeypatch):
    import services.scraper_runner as runner
    import routers.scraper as scraper_router
    calls = []
    events = []
    payload = [{"id": SCRAPERS[0][1], "label": SCRAPERS[0][0], "status": "OK", "seconds": 0.1}]
    monkeypatch.setattr(runner, "RUN_CMD", [sys.executable, "-c", f"print('SCRAPE_RESULT_JSON: {json.dumps(payload)}')"])
    monkeypatch.setattr(scraper_router, "log_audit", lambda *args: events.append(args[1]))

    def run_stub(*args, **kwargs):
        events.append("worker")
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": f"SCRAPE_RESULT_JSON: {json.dumps(payload)}"})()

    monkeypatch.setattr(runner.subprocess, "run", run_stub)

    denied = client.post("/api/scraper/run", headers=auth_for(make_user, mint_token, "pengguna"))
    assert denied.status_code == 403
    assert calls == []

    headers = auth_for(make_user, mint_token, "developer")
    response = client.post("/api/scraper/run", headers=headers)
    assert response.status_code == 202
    for _ in range(20):
        status = client.get("/api/scraper/run", headers=headers).json()
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    assert status["state"] == "completed"
    assert status["results"] == payload
    assert events[:2] == ["SCRAPE_STARTED", "worker"]


def test_scrape_run_marks_source_error_as_failed(client, make_user, mint_token, monkeypatch):
    import services.scraper_runner as runner
    payload = [{"id": SCRAPERS[0][1], "label": SCRAPERS[0][0], "status": "ERROR: source failed", "seconds": 0.1}]
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: type(
            "R", (), {"returncode": 0, "stdout": f"SCRAPE_RESULT_JSON: {json.dumps(payload)}"}
        )(),
    )
    headers = auth_for(make_user, mint_token, "developer")
    assert client.post("/api/scraper/run", headers=headers).status_code == 202
    for _ in range(20):
        status = client.get("/api/scraper/run", headers=headers).json()
        if status["state"] == "failed":
            break
        time.sleep(0.01)
    assert status["state"] == "failed"
    assert status["results"] == payload
    assert status["error"] == "One or more scraper sources failed"


def test_scrape_run_rejects_all_inactive(client, make_user, mint_token, monkeypatch):
    import services.scraper_runner as runner
    monkeypatch.setattr(runner, "RUN_CMD", [sys.executable, "-c", "raise SystemExit(1)"])
    headers = auth_for(make_user, mint_token, "sekretaris perusahaan")
    assert client.put(
        "/api/scraper/config",
        json={"scrapers": [{"id": module, "active": False} for _, module, _ in SCRAPERS]},
        headers=headers,
    ).status_code == 200
    response = client.post("/api/scraper/run", headers=headers)
    assert response.status_code == 400
    assert "Tidak ada sumber aktif" in response.json()["detail"]
