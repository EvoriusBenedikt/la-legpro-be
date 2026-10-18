"""T4: JWT revocation through users.token_version."""
import jwt
import pytest

from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db

PW = "Str0ng!Passw0rd"
NEW_PW = "N3w!Passw0rd-xyz"


def me(client, token):
    return client.get("/api/auth/me", headers=bearer(token))


def tv_of(username):
    return pg_service.query_one(
        "SELECT token_version FROM users WHERE username = %s", (username,))["token_version"]


def test_new_tokens_carry_tv_iat_exp_jti(make_user, login):
    make_user("alice")
    token = login("alice", PW).json()["access_token"]
    claims = jwt.decode(token, "test-secret-not-real", algorithms=["HS256"])
    assert claims["tv"] == 0
    assert {"iat", "exp", "jti", "sub"} <= set(claims)
    other = login("alice", PW).json()["access_token"]
    assert jwt.decode(other, "test-secret-not-real", algorithms=["HS256"])["jti"] != claims["jti"]


def test_register_issues_tv_zero(client):
    r = client.post("/api/auth/register", json={
        "username": "fresh", "email": "fresh@example.test", "password": PW})
    claims = jwt.decode(r.json()["access_token"], "test-secret-not-real", algorithms=["HS256"])
    assert claims["tv"] == 0


def test_change_password_revokes_old_token_and_returns_fresh_one(make_user, client, login):
    make_user("alice")
    old = login("alice", PW).json()["access_token"]
    assert me(client, old).status_code == 200
    r = client.post("/api/auth/change-password", headers=bearer(old),
                    json={"old_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["user"]["username"] == "alice"
    assert tv_of("alice") == 1
    assert me(client, old).status_code == 401
    assert me(client, body["access_token"]).status_code == 200
    assert login("alice", NEW_PW).status_code == 200
    assert login("alice", PW).status_code == 401


def test_admin_reset_password_revokes(make_user, client, mint_token):
    admin = make_user("boss", role="admin")
    target = make_user("alice")
    old = mint_token(target)
    r = client.post(f"/api/admin/users/{target}/reset-password", headers=bearer(mint_token(admin)))
    assert r.status_code == 200
    assert me(client, old).status_code == 401
    assert me(client, mint_token(admin)).status_code == 200


def test_admin_force_logout_revokes(make_user, client, mint_token):
    admin = make_user("boss", role="admin")
    target = make_user("alice")
    old = mint_token(target)
    assert me(client, old).status_code == 200  # creates the active_sessions row
    r = client.post(f"/api/admin/users/{target}/force-logout", headers=bearer(mint_token(admin)))
    assert r.status_code == 200
    assert me(client, old).status_code == 401
    assert pg_service.query_one(
        "SELECT 1 AS x FROM active_sessions WHERE user_id = %s", (target,)) is None
    # the account itself still works: a new login gets a new, valid token
    assert tv_of("alice") == 1


def test_deactivate_revokes_even_after_reactivation(make_user, client, mint_token):
    admin = make_user("boss", role="admin")
    target = make_user("alice")
    old = mint_token(target)
    h = bearer(mint_token(admin))
    assert client.patch(f"/api/admin/users/{target}/status", headers=h,
                        json={"is_active": False}).status_code == 200
    assert me(client, old).status_code == 401
    assert client.patch(f"/api/admin/users/{target}/status", headers=h,
                        json={"is_active": True}).status_code == 200
    assert me(client, old).status_code == 401  # version moved on, token stays dead
    assert me(client, mint_token(target)).status_code == 200


def test_reactivation_alone_does_not_bump(make_user, client, mint_token):
    admin = make_user("boss", role="admin")
    target = make_user("alice", is_active=False)
    assert client.patch(f"/api/admin/users/{target}/status", headers=bearer(mint_token(admin)),
                        json={"is_active": True}).status_code == 200
    assert tv_of("alice") == 0


def test_role_change_revokes(make_user, client, mint_token):
    admin = make_user("boss", role="admin")
    target = make_user("alice")
    old = mint_token(target)
    r = client.patch(f"/api/admin/users/{target}/role", headers=bearer(mint_token(admin)),
                     json={"role": "manajer"})
    assert r.status_code == 200
    assert me(client, old).status_code == 401
    fresh = me(client, mint_token(target))
    assert fresh.status_code == 200 and fresh.json()["role"] == "manajer"


def test_token_without_tv_is_version_zero(make_user, client, mint_token):
    uid = make_user("alice")
    legacy = mint_token(uid, tv=None)  # claim omitted, like pre-deploy tokens
    assert "tv" not in jwt.decode(legacy, "test-secret-not-real", algorithms=["HS256"])
    assert me(client, legacy).status_code == 200
    pg_service.execute("UPDATE users SET token_version = 1 WHERE id = %s", (uid,))
    assert me(client, legacy).status_code == 401


def test_wrong_or_tampered_tv_is_rejected(make_user, client, mint_token):
    uid = make_user("alice")
    assert me(client, mint_token(uid, tv=5)).status_code == 401
    assert me(client, mint_token(uid, tv="abc")).status_code == 401
    assert me(client, mint_token(uid, tv=-1)).status_code == 401
    good = mint_token(uid)
    claims = jwt.decode(good, "test-secret-not-real", algorithms=["HS256"])
    claims["tv"] = 99
    forged = jwt.encode(claims, "another-secret", algorithm="HS256")
    assert me(client, forged).status_code == 401


def test_expired_token_still_rejected(make_user, client):
    import auth
    from datetime import timedelta

    uid = make_user("alice")
    token = auth.create_access_token({"sub": uid, "username": "alice", "tv": 0},
                                     expires_delta=timedelta(seconds=-5))
    r = me(client, token)
    assert r.status_code == 401 and "expired" in r.json()["detail"].lower()
