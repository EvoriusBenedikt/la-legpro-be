"""Admin access-grants panel: list/filter/history, mutations, bulk, enforcement,
audit redaction and the expiry job (migration 012). Real PG, like the other db tests."""
import threading
import uuid

import pytest
from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db

BASE = "/api/admin/access-grants"
PDF_BYTES = b"%PDF-1.4 fake test pdf"
NO_TOKEN = (401, 403)


@pytest.fixture
def env(make_user, mint_token, client):
    class Env:
        pass

    e = Env()
    e.admin = make_user("admin", role="admin")
    e.h = bearer(mint_token(e.admin))
    e.make_user, e.mint_token, e.client = make_user, mint_token, client
    e.regs = []

    def reg(judul="Dok Uji", klasifikasi="Rahasia", local_path=None):
        rid = pg_service.execute_returning(
            "INSERT INTO regulations (judul, nomor, status, klasifikasi, local_path) "
            "VALUES (%s, %s, 'Berlaku', %s, %s) RETURNING id",
            (judul, "n-" + uuid.uuid4().hex[:6], klasifikasi, local_path))[0]["id"]
        e.regs.append(rid)
        return str(rid)

    def grant(to, doc, expires="NULL", reason="alasan rahasia uji", granted_by=None, **cols):
        gid = str(uuid.uuid4())
        pg_service.execute(
            "INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason, expires_at) "
            f"VALUES (%s, %s, %s, %s, %s, {expires})",
            (gid, doc, granted_by or e.admin, to, reason))
        for k, v in cols.items():
            pg_service.execute(f"UPDATE access_grants SET {k} = {v} WHERE id = %s", (gid,))
        pg_service.execute(
            "INSERT INTO access_grant_events (grant_id, event_type, actor_id, granted_to, doc_id) "
            "VALUES (%s, 'granted', %s, %s, %s)", (gid, e.admin, to, doc))
        return gid

    e.reg, e.grant = reg, grant
    yield e
    if e.regs:
        pg_service.execute("DELETE FROM regulations WHERE id = ANY(%s)", (e.regs,))


def events(gid, etype=None):
    sql = "SELECT * FROM access_grant_events WHERE grant_id = %s"
    params = [gid]
    if etype:
        sql += " AND event_type = %s"
        params.append(etype)
    return pg_service.query(sql + " ORDER BY id", params)


def audit(action):
    return pg_service.query("SELECT * FROM audit_logs WHERE action_type = %s", (action,))


# ── list / filters ──────────────────────────────────────────────────────────

def test_list_requires_admin(env):
    assert env.client.get(BASE).status_code in NO_TOKEN
    u = env.make_user("ordinary")
    assert env.client.get(BASE, headers=bearer(env.mint_token(u))).status_code == 403
    for method, path in (("get", BASE + "/ids"), ("get", BASE + "/history"),
                         ("post", BASE + "/bulk")):
        assert getattr(env.client, method)(path).status_code in NO_TOKEN
        assert getattr(env.client, method)(
            path, headers=bearer(env.mint_token(u))).status_code in (403, 422)
    gid = str(uuid.uuid4())
    for method, path in (("post", f"{BASE}/{gid}/revoke"), ("patch", f"{BASE}/{gid}/expiry"),
                         ("post", f"{BASE}/{gid}/reveal-reason")):
        assert getattr(env.client, method)(path).status_code in NO_TOKEN


def test_list_shape_filters_sort_and_count(env):
    a = env.make_user("alice", email="alice@example.test")
    b = env.make_user("bob")
    env.make_user("inactive", is_active=False)
    d1, d2 = env.reg("Peraturan Satu"), env.reg("Peraturan 100%_Dua")
    g_soon = env.grant(a, d1, "NOW() + interval '3 days'")
    g_never = env.grant(b, d2, "NULL", permission="'read'")
    g_far = env.grant(a, d2, "NOW() + interval '60 days'")
    env.grant(b, d1, "NOW() - interval '1 day'")  # expired
    inactive = pg_service.query_one("SELECT id FROM users WHERE username='inactive'")["id"]
    env.grant(inactive, d1, "NULL")

    r = env.client.get(BASE, headers=env.h)
    assert r.status_code == 200
    body = r.json()
    assert {g["id"] for g in body["grants"]} == {g_soon, g_never, g_far}
    assert body["total"] == 3 and body["active_total"] == 3
    assert body["page"] == 1 and body["page_size"] == 25
    assert body["expiring_soon_days"] == 7
    row = next(g for g in body["grants"] if g["id"] == g_soon)
    assert row["expiring_soon"] is True and row["status"] == "active"
    assert row["expires_at"].endswith("Z") and row["created_at"].endswith("Z")
    assert row["last_accessed_at"] is None
    assert row["granted_by_username"] == "admin" and row["permission"] == "full"
    assert row["judul"] == "Peraturan Satu"

    # dashboard tile count == list total (same predicate)
    dash = env.client.get("/api/admin/dashboard", headers=env.h).json()
    assert dash["active_grants"] == body["total"]

    q = lambda **p: env.client.get(BASE, params=p, headers=env.h).json()  # noqa: E731
    assert {g["id"] for g in q(search="alice")["grants"]} == {g_soon, g_far}
    assert {g["id"] for g in q(search="satu")["grants"]} == {g_soon}
    # literal % / _ only match the "100%_" title (both its grants), not everything
    assert {g["id"] for g in q(search="%")["grants"]} == {g_never, g_far}
    assert {g["id"] for g in q(search="_")["grants"]} == {g_never, g_far}
    assert {g["id"] for g in q(permission="read")["grants"]} == {g_never}
    assert {g["id"] for g in q(expiring="true")["grants"]} == {g_soon}
    assert {g["id"] for g in q(never_expires="true")["grants"]} == {g_never}
    assert q(granted_by=env.admin)["total"] == 3
    assert q(granted_by="nobody")["total"] == 0
    assert [g["id"] for g in q(sort="expiry")["grants"]] == [g_soon, g_far, g_never]
    assert [g["username"] for g in q(sort="username")["grants"]] == ["alice", "alice", "bob"]
    paged = q(page_size=2, page=2)
    assert paged["total"] == 3 and len(paged["grants"]) == 1

    exp = q(status="expired")
    assert exp["total"] == 1 and exp["grants"][0]["status"] == "expired"
    assert q(status="expired", expiring="true")["total"] == 0

    ids = env.client.get(BASE + "/ids", params={"permission": "full"}, headers=env.h).json()
    assert set(ids["ids"]) == {g_soon, g_far} and ids["total"] == 2 and ids["truncated"] is False

    for bad in ({"status": "x"}, {"sort": "drop table"}, {"page_size": 101}, {"page": 0},
                {"search": "x" * 101}, {"permission": "admin"}):
        assert env.client.get(BASE, params=bad, headers=env.h).status_code == 422


# ── revoke / history ────────────────────────────────────────────────────────

def test_revoke_then_409_and_history(env):
    u = env.make_user("alice")
    doc = env.reg()
    gid = env.grant(u, doc, "NOW() + interval '5 days'")
    r = env.client.post(f"{BASE}/{gid}/revoke", headers=env.h, json={"note": "tidak perlu"})
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert env.client.get(BASE, headers=env.h).json()["total"] == 0
    assert pg_service.query_one("SELECT revoked_at, revoked_by FROM access_grants WHERE id=%s", (gid,))["revoked_by"] == env.admin
    ev = events(gid, "revoked")
    assert len(ev) == 1 and ev[0]["new_value"] == "tidak perlu" and ev[0]["old_value"] != "none"
    assert len(audit("REVOKE_ACCESS")) == 1
    assert env.client.post(f"{BASE}/{gid}/revoke", headers=env.h).status_code == 409
    assert len(events(gid, "revoked")) == 1 and len(audit("REVOKE_ACCESS")) == 1
    assert env.client.post(f"{BASE}/{uuid.uuid4()}/revoke", headers=env.h).status_code == 404
    assert env.client.post(f"{BASE}/not-a-uuid/revoke", headers=env.h).status_code == 422

    hist = env.client.get(BASE + "/history", headers=env.h).json()
    assert hist["total"] == 2  # granted + revoked
    assert hist["events"][0]["event_type"] == "revoked"
    only = env.client.get(BASE + "/history", params={"event_type": "revoked"}, headers=env.h).json()
    assert only["total"] == 1
    assert env.client.get(BASE + "/history", params={"event_type": "nope"}, headers=env.h).status_code == 422
    assert env.client.get(BASE + "/history", params={"date_from": "bad"}, headers=env.h).status_code == 400
    assert env.client.get(BASE + "/history", params={"search": "alice", "page_size": 1},
                          headers=env.h).json()["total"] == 1  # revoked event carries grantee_name


def test_history_search_and_paging(env):
    u = env.make_user("carol")
    gid = env.grant(u, env.reg("Dokumen Cari"))
    pg_service.execute(
        "UPDATE access_grant_events SET grantee_name='carol', doc_title='Dokumen Cari' WHERE grant_id=%s", (gid,))
    h = env.client.get(BASE + "/history", params={"search": "carol", "page_size": 1}, headers=env.h).json()
    assert h["total"] == 1 and len(h["events"]) == 1
    assert env.client.get(BASE + "/history", params={"search": "%"}, headers=env.h).json()["total"] == 0


# ── expiry ──────────────────────────────────────────────────────────────────

def test_expiry_modes_and_validation(env):
    u = env.make_user("alice")
    gid = env.grant(u, env.reg(), "NOW() + interval '2 days'")
    url = f"{BASE}/{gid}/expiry"
    patch = lambda body: env.client.patch(url, headers=env.h, json=body)  # noqa: E731

    assert patch({"mode": "extend", "days": 30}).status_code == 200
    ev = events(gid, "extended")
    assert len(ev) == 1 and ev[0]["old_value"] and ev[0]["new_value"] != "none"
    days = pg_service.query_one(
        "SELECT EXTRACT(EPOCH FROM (expires_at - NOW()))/86400 AS d FROM access_grants WHERE id=%s", (gid,))["d"]
    assert 31.9 < float(days) < 32.1  # extended from the future expiry

    assert patch({"mode": "set", "expires_at": "2000-01-01T00:00:00Z"}).status_code == 400
    assert patch({"mode": "set", "expires_at": "2099-01-01T00:00:00"}).status_code == 400  # naive
    assert patch({"mode": "set", "expires_at": "2099-01-01T00:00:00Z"}).status_code == 400  # > 5y
    assert patch({"mode": "extend", "days": 0}).status_code == 422
    assert patch({"mode": "extend", "days": 400}).status_code == 422
    assert patch({"mode": "bogus"}).status_code == 422
    assert patch({"mode": "set"}).status_code == 400

    future = (pg_service.query_one("SELECT to_char(NOW() + interval '90 days', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"') AS t")["t"])
    assert patch({"mode": "set", "expires_at": future}).status_code == 200
    assert len(events(gid, "expiry_set")) >= 1

    assert patch({"mode": "never"}).status_code == 200
    assert pg_service.query_one("SELECT expires_at FROM access_grants WHERE id=%s", (gid,))["expires_at"] is None
    assert len(events(gid, "expiry_cleared")) == 1
    assert len(audit("UPDATE_ACCESS_EXPIRY")) >= 3

    env.client.post(f"{BASE}/{gid}/revoke", headers=env.h)
    assert patch({"mode": "never"}).status_code == 409


def test_extend_expired_grant_reactivates_from_now(env):
    u = env.make_user("alice")
    gid = env.grant(u, env.reg(), "NOW() - interval '10 days'")
    assert env.client.get(BASE, headers=env.h).json()["total"] == 0
    assert env.client.get(BASE, params={"status": "expired"}, headers=env.h).json()["total"] == 1
    assert env.client.patch(f"{BASE}/{gid}/expiry", headers=env.h,
                            json={"mode": "extend", "days": 7}).status_code == 200
    d = pg_service.query_one(
        "SELECT EXTRACT(EPOCH FROM (expires_at - NOW()))/86400 AS d FROM access_grants WHERE id=%s", (gid,))["d"]
    assert 6.9 < float(d) < 7.1
    assert env.client.get(BASE, headers=env.h).json()["total"] == 1


def test_mutation_works_when_document_row_is_missing(env):
    u = env.make_user("alice")
    gid = env.grant(u, "999999991")  # no such regulation -> LEFT JOIN yields NULLs
    assert env.client.patch(f"{BASE}/{gid}/permission", headers=env.h,
                            json={"permission": "read"}).status_code == 200
    assert env.client.post(f"{BASE}/{gid}/revoke", headers=env.h).status_code == 200


# ── permission / reason visibility ──────────────────────────────────────────

def test_permission_and_visibility_and_reveal(env):
    u = env.make_user("alice")
    mgr = env.make_user("mgr", role="manajer")
    gid = env.grant(u, env.reg(), reason="alasan sangat rahasia")
    assert env.client.patch(f"{BASE}/{gid}/permission", headers=env.h,
                            json={"permission": "admin"}).status_code == 422
    assert env.client.patch(f"{BASE}/{gid}/permission", headers=env.h,
                            json={"permission": "read"}).status_code == 200
    assert len(events(gid, "permission_changed")) == 1
    env.client.patch(f"{BASE}/{gid}/permission", headers=env.h, json={"permission": "read"})
    assert len(events(gid, "permission_changed")) == 1  # no-op writes nothing
    assert len(audit("UPDATE_ACCESS_PERMISSION")) == 1

    # visible by default
    row = env.client.get(BASE, headers=env.h).json()["grants"][0]
    assert row["reason"] == "alasan sangat rahasia" and row["reason_hidden"] is False

    assert env.client.patch(f"{BASE}/{gid}/reason-visibility", headers=env.h,
                            json={"reason_visible": False}).status_code == 200
    assert len(events(gid, "visibility_changed")) == 1
    assert len(audit("UPDATE_ACCESS_REASON_VISIBILITY")) == 1
    list_json = env.client.get(BASE, headers=env.h).text
    assert "alasan sangat rahasia" not in list_json
    row = env.client.get(BASE, headers=env.h).json()["grants"][0]
    assert row["reason"] is None and row["reason_hidden"] is True
    hist_text = env.client.get(BASE + "/history", headers=env.h).text
    assert "alasan sangat rahasia" not in hist_text
    # non-admin grants endpoint (the granting manajer sees the masked reason)
    pg_service.execute("UPDATE access_grants SET granted_by=%s WHERE id=%s", (mgr, gid))
    r = env.client.get("/api/repository/grants", headers=bearer(env.mint_token(mgr)))
    assert r.status_code == 200
    assert "alasan sangat rahasia" not in r.text
    mine = r.json()["grants"][0]
    assert mine["reason"] is None and mine["reason_hidden"] is True

    rv = env.client.post(f"{BASE}/{gid}/reveal-reason", headers=env.h)
    assert rv.status_code == 200 and rv.json()["reason"] == "alasan sangat rahasia"
    assert len(audit("REVEAL_GRANT_REASON")) == 1
    assert "alasan sangat rahasia" not in str(audit("REVEAL_GRANT_REASON"))
    assert len(events(gid, "reason_revealed")) == 1
    # reveal events are excluded from the default history
    types = {e["event_type"] for e in env.client.get(BASE + "/history", headers=env.h).json()["events"]}
    assert "reason_revealed" not in types


# ── bulk ────────────────────────────────────────────────────────────────────

def test_bulk_partial_failure_and_validation(env):
    u = env.make_user("alice")
    doc = env.reg()
    g1, g2, g3 = (env.grant(u, doc) for _ in range(3))
    env.client.post(f"{BASE}/{g2}/revoke", headers=env.h)
    missing = str(uuid.uuid4())

    r = env.client.post(BASE + "/bulk", headers=env.h, json={"action": "revoke", "ids": [g1, g2, missing, g1]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok_count"] == 1 and body["fail_count"] == 2
    by_id = {x["id"]: x for x in body["results"]}
    assert by_id[g1]["ok"] is True
    assert by_id[g2]["code"] == "already_revoked" and by_id[missing]["code"] == "not_found"
    assert pg_service.query_one("SELECT revoked_at FROM access_grants WHERE id=%s", (g1,))["revoked_at"] is not None

    ext = env.client.post(BASE + "/bulk", headers=env.h, json={
        "action": "extend", "ids": [g3], "expiry": {"mode": "extend", "days": 30}})
    assert ext.json()["ok_count"] == 1
    bad = env.client.post(BASE + "/bulk", headers=env.h, json={
        "action": "extend", "ids": [g3], "expiry": {"mode": "set", "expires_at": "2000-01-01T00:00:00Z"}})
    assert bad.json()["results"][0]["code"] == "invalid_expiry"
    vis = env.client.post(BASE + "/bulk", headers=env.h, json={
        "action": "reason_visibility", "ids": [g3], "reason_visible": False})
    assert vis.json()["ok_count"] == 1

    for payload in ({"action": "revoke", "ids": ["bad-id"]}, {"action": "revoke", "ids": []},
                    {"action": "revoke", "ids": [str(uuid.uuid4()) for _ in range(101)]},
                    {"action": "nuke", "ids": [g3]}):
        assert env.client.post(BASE + "/bulk", headers=env.h, json=payload).status_code == 422
    assert env.client.post(BASE + "/bulk", headers=env.h, json={"action": "extend", "ids": [g3]}).status_code == 422
    assert env.client.post(BASE + "/bulk", headers=env.h,
                           json={"action": "reason_visibility", "ids": [g3]}).status_code == 422


def test_bulk_concurrent_reversed_id_lists_do_not_deadlock(env):
    u = env.make_user("alice")
    doc = env.reg()
    ids = [env.grant(u, doc) for _ in range(12)]
    results = []

    def run(order):
        r = env.client.post(BASE + "/bulk", headers=env.h, json={
            "action": "extend", "ids": order, "expiry": {"mode": "extend", "days": 1}})
        results.append(r.status_code)

    t1 = threading.Thread(target=run, args=(ids,))
    t2 = threading.Thread(target=run, args=(list(reversed(ids)),))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    assert results == [200, 200]


# ── legacy endpoints ────────────────────────────────────────────────────────

def test_legacy_delete_soft_revokes_idempotently(env):
    sek = env.make_user("sek", role="sekretaris perusahaan")
    u = env.make_user("alice")
    gid = env.grant(u, env.reg())
    h = bearer(env.mint_token(sek))
    r1 = env.client.delete(f"/api/repository/grant/{gid}", headers=h)
    assert r1.status_code == 200 and r1.json() == {"message": "Akses berhasil dicabut."}
    r2 = env.client.delete(f"/api/repository/grant/{gid}", headers=h)
    assert r2.status_code == 409 and r2.json()["detail"] == "Akses sudah dicabut."
    assert len(events(gid, "revoked")) == 1 and len(audit("REVOKE_ACCESS")) == 1
    assert pg_service.query_one("SELECT 1 AS n FROM access_grants WHERE id=%s", (gid,)) is not None
    assert env.client.delete(f"/api/repository/grant/{uuid.uuid4()}", headers=h).status_code == 404
    assert env.client.delete("/api/repository/grant/unknown", headers=h).status_code == 404


def test_grant_creation_writes_event_and_no_reason_in_audit(env):
    mgr = env.make_user("mgr", role="manajer")
    u = env.make_user("alice")
    doc = env.reg(klasifikasi="Rahasia")
    r = env.client.post(f"/api/documents/{doc}/grant-access", headers=bearer(env.mint_token(mgr)),
                        json={"granted_to": u, "reason": "alasan unik 12345"})
    assert r.status_code == 200 and r.json() == {"message": "Akses berhasil diberikan."}
    gid = pg_service.query_one("SELECT id FROM access_grants WHERE granted_to=%s", (u,))["id"]
    ev = events(gid, "granted")
    assert len(ev) == 1 and ev[0]["actor_name"] == "mgr" and ev[0]["grantee_name"] == "alice"
    rows = audit("GRANT_ACCESS")
    assert len(rows) == 1 and "alasan unik" not in rows[0]["details"]


# ── audit readers: legacy reasons are masked everywhere ─────────────────────

def test_legacy_reason_redacted_in_all_audit_readers(env):
    eng = env.make_user("eng", role="insinyur ti")
    target = env.make_user("target")
    pg_service.execute(
        "INSERT INTO audit_logs (user_id, action_type, resource_id, details) VALUES (%s,'GRANT_ACCESS','1',%s)",
        (target, "Diberikan kepada: x, Dokumen: D, Alasan: legacy-secret-reason"))
    pg_service.execute(
        "INSERT INTO audit_logs (user_id, action_type, resource_id, details) VALUES (%s,'OTHER','1',%s)",
        (target, "plain legacy-secret-reason visible"))

    admin_logs = env.client.get("/api/admin/audit-logs", headers=env.h)
    assert "Diberikan kepada: x, Dokumen: D" in admin_logs.text
    grant_row = [l for l in admin_logs.json()["logs"] if l["action_type"] == "GRANT_ACCESS"][0]
    assert "legacy-secret-reason" not in grant_row["details"]
    exp = env.client.get("/api/admin/audit-logs/export", params={"format": "json"}, headers=env.h)
    assert all("legacy-secret-reason" not in x["details"] for x in exp.json() if x["action_type"] == "GRANT_ACCESS")
    csv_txt = env.client.get("/api/admin/audit-logs/export", headers=env.h).text
    assert "Alasan:" not in csv_txt

    detail = env.client.get(f"/api/admin/users/{target}", headers=env.h)
    # user detail is exact-role admin; may be 200 or 403 for sekretaris, admin is fine
    assert detail.status_code == 200
    assert all("legacy-secret-reason" not in (a["details"] or "")
               for a in detail.json()["recent_activity"] if a["action_type"] == "GRANT_ACCESS")

    eng_logs = env.client.get("/api/engineer/audit-logs", headers=bearer(env.mint_token(eng)))
    assert eng_logs.status_code == 200
    assert all("legacy-secret-reason" not in l["details"] for l in eng_logs.json()["logs"] if l["action_type"] == "GRANT_ACCESS")

    # search oracle: GRANT_ACCESS rows never match on the reason; other rows still do
    a = env.client.get("/api/admin/audit-logs", params={"search": "legacy-secret"}, headers=env.h).json()
    assert [l["action_type"] for l in a["logs"]] == ["OTHER"]
    e = env.client.get("/api/engineer/audit-logs", params={"search": "legacy-secret"},
                       headers=bearer(env.mint_token(eng))).json()
    assert [l["action_type"] for l in e["logs"]] == ["OTHER"]
    a2 = env.client.get("/api/admin/audit-logs", params={"search": "Diberikan kepada"}, headers=env.h).json()
    assert a2["total"] == 1


# ── enforcement ─────────────────────────────────────────────────────────────

@pytest.fixture
def pdf_doc(env, tmp_path, monkeypatch):
    from routers import repository
    monkeypatch.setattr(repository, "PDFS_DIR", str(tmp_path))
    (tmp_path / "sec.pdf").write_bytes(PDF_BYTES)
    return env.reg("Rahasia Terbatas", "Terbatas", local_path=str(tmp_path / "sec.pdf"))


def test_pdf_and_repository_enforcement(env, pdf_doc):
    u = env.make_user("alice")
    h = bearer(env.mint_token(u))
    assert env.client.get("/api/pdf/sec.pdf", headers=h).status_code == 403
    gid = env.grant(u, pdf_doc, "NOW() + interval '1 day'")
    assert env.client.get("/api/pdf/sec.pdf", headers=h).status_code == 200
    assert pg_service.query_one("SELECT last_accessed_at FROM access_grants WHERE id=%s", (gid,))["last_accessed_at"] is not None
    assert pdf_doc in {d["id"] for d in env.client.get("/api/repository", headers=h).json()["documents"]}

    # read-permission still opens the PDF and lists the doc
    env.client.patch(f"{BASE}/{gid}/permission", headers=env.h, json={"permission": "read"})
    assert env.client.get("/api/pdf/sec.pdf", headers=h).status_code == 200
    assert pdf_doc in {d["id"] for d in env.client.get("/api/repository", headers=h).json()["documents"]}

    env.client.post(f"{BASE}/{gid}/revoke", headers=env.h)
    assert env.client.get("/api/pdf/sec.pdf", headers=h).status_code == 403
    assert pdf_doc not in {d["id"] for d in env.client.get("/api/repository", headers=h).json()["documents"]}


def test_expired_grant_denied_and_last_accessed_null_when_unused(env, pdf_doc):
    u = env.make_user("alice")
    h = bearer(env.mint_token(u))
    gid = env.grant(u, pdf_doc, "NOW() - interval '1 minute'")
    assert env.client.get("/api/pdf/sec.pdf", headers=h).status_code == 403
    assert pg_service.query_one("SELECT last_accessed_at FROM access_grants WHERE id=%s", (gid,))["last_accessed_at"] is None


def test_rag_retrieval_respects_revoked_expired_and_read(env, monkeypatch):
    from services import rag_service

    doc = env.reg("Dok RAG", "Terbatas")
    u = env.make_user("alice")
    user = {"id": u, "role": "pengguna"}
    vec = "[" + ",".join(["1"] + ["0"] * 383) + "]"
    chunk_id = "chunk-" + uuid.uuid4().hex
    pg_service.execute(
        "INSERT INTO chunks (id, doc_id, text, visibility, embedding, judul) "
        "VALUES (%s, %s, 'isi dokumen terbatas', 'public', %s::vector, 'Dok RAG')", (chunk_id, doc, vec))
    monkeypatch.setattr(rag_service, "embed_query", lambda q: vec)
    monkeypatch.setattr(rag_service, "get_reranker", lambda: (_ for _ in ()).throw(RuntimeError("no reranker")))
    try:
        assert rag_service.retrieve_contexts("zzqq", user) == []
        gid = env.grant(u, doc, "NOW() + interval '1 day'")
        got = rag_service.retrieve_contexts("zzqq", user)
        assert [c["doc_id"] for c in got] == [doc]
        assert pg_service.query_one("SELECT last_accessed_at FROM access_grants WHERE id=%s", (gid,))["last_accessed_at"] is not None

        pg_service.execute("UPDATE access_grants SET permission='read' WHERE id=%s", (gid,))
        assert rag_service.retrieve_contexts("zzqq", user) == []

        pg_service.execute("UPDATE access_grants SET permission='full', revoked_at=NOW() WHERE id=%s", (gid,))
        assert rag_service.retrieve_contexts("zzqq", user) == []

        pg_service.execute("UPDATE access_grants SET revoked_at=NULL, expires_at=NOW() - interval '1 minute' WHERE id=%s", (gid,))
        assert rag_service.retrieve_contexts("zzqq", user) == []
    finally:
        pg_service.execute("DELETE FROM chunks WHERE id = %s", (chunk_id,))


# ── scheduler ───────────────────────────────────────────────────────────────

def test_expire_job_is_idempotent_and_skips_revoked(env):
    from services import alert_scheduler

    u = env.make_user("alice")
    doc = env.reg()
    expired = env.grant(u, doc, "NOW() - interval '2 hours'")
    revoked = env.grant(u, doc, "NOW() - interval '2 hours'", revoked_at="NOW()")
    live = env.grant(u, doc, "NOW() + interval '2 hours'")

    alert_scheduler.expire_access_grants()
    alert_scheduler.expire_access_grants()
    assert len(events(expired, "expired")) == 1
    assert len(events(revoked, "expired")) == 0
    assert len(events(live, "expired")) == 0
    ev = events(expired, "expired")[0]
    assert ev["actor_id"] is None and ev["grantee_name"] == "alice"

    # extending re-arms the job: the next expiry logs exactly one more event
    env.client.patch(f"{BASE}/{expired}/expiry", headers=env.h, json={"mode": "extend", "days": 1})
    assert pg_service.query_one("SELECT expired_logged_at FROM access_grants WHERE id=%s", (expired,))["expired_logged_at"] is None
    pg_service.execute("UPDATE access_grants SET expires_at = NOW() - interval '1 minute' WHERE id=%s", (expired,))
    alert_scheduler.expire_access_grants()
    assert len(events(expired, "expired")) == 2


def test_migration_012_is_idempotent_and_adds_no_duplicates(env):
    import conftest
    u = env.make_user("alice")
    gid = env.grant(u, env.reg(), "NOW() - interval '1 day'")
    before = pg_service.query_one("SELECT count(*) AS n FROM access_grant_events")["n"]
    conftest.apply_migrations(conftest.TEST_DB, upto=12)
    conftest.apply_migrations(conftest.TEST_DB, upto=12)
    dup = pg_service.query("SELECT grant_id, event_type, count(*) FROM access_grant_events "
                           "GROUP BY 1,2 HAVING count(*) > 1")
    assert dup == []
    assert pg_service.query_one("SELECT count(*) AS n FROM access_grant_events")["n"] >= before
    assert pg_service.query_one("SELECT permission, reason_visible FROM access_grants WHERE id=%s", (gid,)) == {
        "permission": "full", "reason_visible": True}
