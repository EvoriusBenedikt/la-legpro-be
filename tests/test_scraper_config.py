import pytest
import pytest

pytestmark = pytest.mark.db

from run_all_scrapers import SCRAPERS


def auth_for(make_user, mint_token, role):
    return {"Authorization": f"Bearer {mint_token(make_user(role, role=role))}"}


def test_auth_roles(client, make_user, mint_token):
    response = client.get("/api/scraper/config")
    assert response.status_code in (401, 403)
    scraper_id = SCRAPERS[0][1]
    denied_roles = ("pengguna", "manajer", "direktur", "admin", "insinyur ti")
    for role in denied_roles:
        headers = auth_for(make_user, mint_token, role)
        assert client.get("/api/scraper/config", headers=headers).status_code == 403
        assert client.put(
            "/api/scraper/config",
            json={"scrapers": [{"id": scraper_id, "active": False}]},
            headers=headers,
        ).status_code == 403
    for role in ("sekretaris perusahaan", "dewa"):
        assert client.get("/api/scraper/config", headers=auth_for(make_user, mint_token, role)).status_code == 200


def test_developer_can_get_and_put(client, make_user, mint_token):
    headers = auth_for(make_user, mint_token, "developer")
    scraper_id = SCRAPERS[0][1]
    assert client.get("/api/scraper/config", headers=headers).status_code == 200
    response = client.put(
        "/api/scraper/config",
        json={"scrapers": [{"id": scraper_id, "active": False}]},
        headers=headers,
    )
    assert response.status_code == 200
    assert next(item for item in response.json()["scrapers"] if item["id"] == scraper_id)["active"] is False


def test_default_order_and_shape(client, make_user, mint_token):
    headers = auth_for(make_user, mint_token, "sekretaris perusahaan")
    response = client.get("/api/scraper/config", headers=headers)
    assert response.status_code == 200
    actual = response.json()["scrapers"]
    assert [(item["id"], item["name"]) for item in actual] == [(module, label) for label, module, _ in SCRAPERS]
    assert all(item["active"] is True and set(item) == {"id", "name", "active"} for item in actual)


def test_unauthorized_put_is_rejected_without_writing(client, make_user, mint_token):
    headers = auth_for(make_user, mint_token, "admin")
    scraper_id = SCRAPERS[0][1]
    response = client.put(
        "/api/scraper/config",
        json={"scrapers": [{"id": scraper_id, "active": False}]},
        headers=headers,
    )
    assert response.status_code == 403
    from services import pg_service
    assert pg_service.query_one(
        "SELECT COUNT(*) AS n FROM scraper_config WHERE scraper_id = %s", (scraper_id,)
    )["n"] == 0


def test_put_persists_and_audits_only_changes(client, make_user, mint_token):
    user = make_user("scraper-admin", role="sekretaris perusahaan")
    headers = {"Authorization": f"Bearer {mint_token(user)}"}
    scraper_id = SCRAPERS[0][1]
    response = client.put("/api/scraper/config", json={"scrapers": [{"id": scraper_id, "active": False}]}, headers=headers)
    assert response.status_code == 200
    assert next(item for item in response.json()["scrapers"] if item["id"] == scraper_id)["active"] is False
    assert next(item for item in client.get("/api/scraper/config", headers=headers).json()["scrapers"] if item["id"] == scraper_id)["active"] is False
    assert client.put("/api/scraper/config", json={"scrapers": [{"id": scraper_id, "active": False}]}, headers=headers).status_code == 200
    from services import pg_service
    assert pg_service.query_one(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action_type = 'UPDATE_SCRAPER_CONFIG' AND resource_id = %s",
        (scraper_id,),
    )["n"] == 1
    import run_all_scrapers
    assert run_all_scrapers._inactive_ids() == {scraper_id}
    assert client.put("/api/scraper/config", json={"scrapers": [{"id": scraper_id, "active": True}]}, headers=headers).status_code == 200


def test_invalid_updates_write_nothing(client, make_user, mint_token):
    headers = auth_for(make_user, mint_token, "sekretaris perusahaan")
    known = SCRAPERS[0][1]
    for body, status, detail in (
        ({"scrapers": [{"id": "no-such-scraper", "active": False}]}, 400, "Scraper tidak dikenal"),
        ({"scrapers": [{"id": known, "active": False}, {"id": known, "active": True}]}, 400, "Scraper duplikat"),
        ({"scrapers": [{"id": known, "active": "false"}]}, 422, None),
        ({}, 422, None),
    ):
        response = client.put("/api/scraper/config", json=body, headers=headers)
        assert response.status_code == status
        if detail:
            assert detail in response.json()["detail"]
    from services import pg_service
    assert pg_service.query_one("SELECT COUNT(*) AS n FROM scraper_config")["n"] == 0


def test_missing_table_returns_503(client, make_user, mint_token, monkeypatch):
    headers = auth_for(make_user, mint_token, "sekretaris perusahaan")
    from psycopg import errors as pg_errors
    from services import pg_service

    monkeypatch.setattr(pg_service, "query", lambda sql: (_ for _ in ()).throw(pg_errors.UndefinedTable()))
    assert client.get("/api/scraper/config", headers=headers).status_code == 503

    class MissingTableConnection:
        def __enter__(self):
            raise pg_errors.UndefinedTable()
        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(pg_service, "get_conn", lambda: MissingTableConnection())
    response = client.put(
        "/api/scraper/config",
        json={"scrapers": [{"id": SCRAPERS[0][1], "active": False}]},
        headers=headers,
    )
    assert response.status_code == 503


def test_runner_selection_and_import_without_bootstrap():
    import run_all_scrapers
    assert run_all_scrapers._selected(SCRAPERS, {SCRAPERS[0][1]}) == SCRAPERS[1:]


def test_runner_reads_persisted_inactive_flag(client, make_user, mint_token, monkeypatch, capsys):
    import sys
    import types
    import run_all_scrapers

    scraper_id = SCRAPERS[0][1]
    headers = auth_for(make_user, mint_token, "sekretaris perusahaan")
    assert client.put(
        "/api/scraper/config",
        json={"scrapers": [{"id": scraper_id, "active": False}]},
        headers=headers,
    ).status_code == 200

    class StubScraper:
        def scrape(self, limit=100):
            raise AssertionError("inactive scraper was executed")

    module = types.ModuleType("scraper_stub")
    module.StubScraper = StubScraper
    monkeypatch.setitem(sys.modules, "scraper_stub", module)
    monkeypatch.setattr(run_all_scrapers, "SCRAPERS", [("Stub", scraper_id, "StubScraper")])
    monkeypatch.setattr(run_all_scrapers.os, "system", lambda command: 0)

    run_all_scrapers.run_all()
    output = capsys.readouterr().out
    assert "Running 0 of 1 scrapers" in output
    assert "[SKIP] Stub (inactive)" in output


def test_runner_reports_skipped_without_failure(monkeypatch, capsys):
    import sys
    import types
    import run_all_scrapers

    ran = []
    active_module = types.ModuleType("active_stub")

    class ActiveScraper:
        def scrape(self, limit=100):
            ran.append(limit)

    active_module.ActiveScraper = ActiveScraper
    monkeypatch.setitem(sys.modules, "active_stub", active_module)
    monkeypatch.setattr(
        run_all_scrapers,
        "SCRAPERS",
        [("Active", "active_stub", "ActiveScraper"), ("Inactive", "inactive_stub", "InactiveScraper")],
    )
    monkeypatch.setattr(run_all_scrapers, "_inactive_ids", lambda: {"inactive_stub"})
    monkeypatch.setattr(run_all_scrapers.os, "system", lambda command: 0)

    run_all_scrapers.run_all()
    output = capsys.readouterr().out
    assert ran == [100]
    assert "Running 1 of 2 scrapers" in output
    assert "[SKIP] Inactive (inactive)" in output
    assert "[--] Inactive" in output
