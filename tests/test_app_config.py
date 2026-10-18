"""docs_kwargs() (no DB) and the D9 uvicorn proxy-header allow-list check."""
from services.app_config import docs_kwargs


def test_docs_off_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    assert docs_kwargs() == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    monkeypatch.setenv("ENABLE_DOCS", "false")
    assert docs_kwargs()["docs_url"] is None
    monkeypatch.setenv("ENABLE_DOCS", "1")
    assert docs_kwargs()["docs_url"] is None


def test_docs_on_when_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_DOCS", "True")
    assert docs_kwargs() == {"docs_url": "/docs", "redoc_url": "/redoc",
                             "openapi_url": "/openapi.json"}


def test_fastapi_app_hides_docs_by_default(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    client = TestClient(FastAPI(**docs_kwargs()))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404

    monkeypatch.setenv("ENABLE_DOCS", "true")
    client = TestClient(FastAPI(**docs_kwargs()))
    assert client.get("/openapi.json").status_code == 200


def test_uvicorn_accepts_cidr_forwarded_allow_ips(monkeypatch):
    """D9: uvicorn 0.52.1 must read FORWARDED_ALLOW_IPS natively and accept
    CIDR entries (Dockerfile/compose rely on it; never fall back to '*')."""
    import uvicorn
    from uvicorn.config import Config
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1")
    config = Config("api.main:app", proxy_headers=True)
    assert config.forwarded_allow_ips == "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1"

    async def app(scope, receive, send):  # pragma: no cover - never invoked
        pass

    mw = ProxyHeadersMiddleware(app, trusted_hosts=config.forwarded_allow_ips)
    trusted = mw.trusted_hosts
    assert "*" not in str(trusted)
    # Behavioural proof via the middleware's own membership test.
    assert "172.18.0.5" in trusted
    assert "10.9.9.9" in trusted
    assert "192.168.5.5" in trusted
    assert "127.0.0.1" in trusted
    assert "203.0.113.9" not in trusted
    assert "8.8.8.8" not in trusted
