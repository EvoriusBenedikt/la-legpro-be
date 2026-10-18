"""T5: audit rows for the six auth events, ip= everywhere, no passwords."""
import pytest

from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db

PW = "Str0ng!Passw0rd"
NEW_PW = "N3w!Passw0rd-xyz"
IP = "198.51.100.77"


def rows(action):
    return pg_service.query(
        "SELECT user_id, action_type, resource_id, details FROM audit_logs "
        "WHERE action_type = %s ORDER BY id", (action,))


def test_login_success_row(make_user, login):
    uid = make_user("alice")
    assert login("alice", PW, IP).status_code == 200
    (row,) = rows("LOGIN_SUCCESS")
    assert row["user_id"] == uid and row["resource_id"] == "alice"
    assert f"ip={IP}" in row["details"] and 'ua="' in row["details"]


def test_login_failure_rows_have_count_and_ip_but_no_password(make_user, login):
    uid = make_user("alice")
    secret = "my-very-secret-guess"
    login("alice", secret, IP)
    login("alice", secret, IP)
    login("nobody", secret, IP)
    known = rows("LOGIN_FAILURE")
    assert len(known) == 3
    assert known[0]["user_id"] == uid and known[0]["resource_id"] == "alice"
    assert f"ip={IP}" in known[0]["details"] and "count=1" in known[0]["details"]
    assert "count=2" in known[1]["details"] and "reason=bad_credentials" in known[1]["details"]
    assert known[2]["user_id"] == "" and known[2]["resource_id"] == "nobody"
    dump = " ".join(" ".join(str(v) for v in r.values()) for r in pg_service.query("SELECT * FROM audit_logs"))
    assert secret not in dump and PW not in dump


def test_account_locked_once_per_lock_event(make_user, login):
    uid = make_user("alice")
    for _ in range(5):
        login("alice", "bad", IP)
    (lock,) = rows("ACCOUNT_LOCKED")
    assert lock["user_id"] == uid and lock["resource_id"] == "alice"
    assert f"ip={IP}" in lock["details"] and "minutes=1" in lock["details"] and "count=5" in lock["details"]
    # attempts while locked: LOGIN_FAILURE reason=locked, but no new lock event
    login("alice", PW, IP)
    login("alice", "bad", IP)
    assert len(rows("ACCOUNT_LOCKED")) == 1
    locked = [r for r in rows("LOGIN_FAILURE") if "reason=locked" in r["details"]]
    assert len(locked) == 2 and all("count=" not in r["details"] for r in locked)
    # a new lock after expiry is a second event with the doubled duration
    pg_service.execute("UPDATE users SET locked_until = now() - interval '1 second'")
    login("alice", "bad", IP)
    events = rows("ACCOUNT_LOCKED")
    assert len(events) == 2 and "minutes=2" in events[1]["details"]


def test_unknown_username_lock_event_uses_empty_user_id(login):
    for _ in range(5):
        login("ghost", "bad", IP)
    (lock,) = rows("ACCOUNT_LOCKED")
    assert lock["user_id"] == "" and lock["resource_id"] == "ghost"


def test_register_and_password_change_rows(client, make_user, mint_token):
    r = client.post("/api/auth/register",
                    json={"username": "fresh", "email": "fresh@example.test", "password": PW},
                    headers={"X-Real-IP": IP})
    uid = r.json()["user"]["id"]
    (reg,) = rows("REGISTER")
    assert reg["user_id"] == uid and reg["resource_id"] == "fresh" and f"ip={IP}" in reg["details"]

    token = r.json()["access_token"]
    r = client.post("/api/auth/change-password", headers={**bearer(token), "X-Real-IP": IP},
                    json={"old_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200
    (chg,) = rows("PASSWORD_CHANGE")
    assert chg["user_id"] == uid and chg["resource_id"] == "fresh" and f"ip={IP}" in chg["details"]
    dump = " ".join(str(r["details"]) for r in pg_service.query("SELECT details FROM audit_logs"))
    assert PW not in dump and NEW_PW not in dump


def test_unlock_user_row(client, make_user, mint_token):
    admin = make_user("boss", role="admin")
    target = make_user("victim")
    client.post(f"/api/admin/users/{target}/unlock",
                headers={**bearer(mint_token(admin)), "X-Real-IP": IP})
    (row,) = rows("UNLOCK_USER")
    assert row["user_id"] == admin and row["resource_id"] == target
    assert "victim" in row["details"] and f"ip={IP}" in row["details"]


def test_attempted_username_is_sanitised_and_truncated(login):
    # <= 64 chars (the login model caps the field); the 64-char truncation
    # itself is covered by test_client_ip.test_sanitize_username.
    evil = "bob\nLOGIN_SUCCESS ip=6.6.6.6\r\x1b[31m"
    r = login(evil, "bad", IP)
    assert r.status_code == 401
    (row,) = rows("LOGIN_FAILURE")
    assert row["resource_id"] == "bobLOGIN_SUCCESS ip=6.6.6.6[31m"
    assert "\n" not in row["resource_id"] and "\r" not in row["resource_id"]
    assert "\x1b" not in row["resource_id"]
    assert len(row["resource_id"]) <= 64
    assert "\n" not in row["details"]


def test_active_session_stores_ip_address(make_user, client, mint_token):
    uid = make_user("alice")
    r = client.get("/api/auth/me", headers={**bearer(mint_token(uid)), "X-Real-IP": IP})
    assert r.status_code == 200
    row = pg_service.query_one("SELECT ip_address FROM active_sessions WHERE user_id = %s", (uid,))
    assert row["ip_address"] == IP
