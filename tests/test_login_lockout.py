"""T3 + T6: escalating lockout, uniform responses, admin unlock."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db

PW = "Str0ng!Passw0rd"
BAD = "wrong-password"
IP = "198.51.100.10"


def urow(username):
    return pg_service.query_one(
        "SELECT failed_attempts, locked_until, last_failed_at, "
        "CEIL(EXTRACT(EPOCH FROM (locked_until - now())))::int AS remaining "
        "FROM users WHERE username = %s", (username,))


def retry_after(resp):
    return int(resp.headers["retry-after"])


def expire_lock(username):
    pg_service.execute(
        "UPDATE users SET locked_until = now() - interval '1 second' WHERE username = %s", (username,))


# ---- threshold and escalation -------------------------------------------

def test_four_failures_401_then_fifth_locks_with_429(make_user, login):
    make_user("victim")
    for _ in range(4):
        r = login("victim", BAD, IP)
        assert r.status_code == 401
        assert r.json()["detail"] == "Incorrect username or password"
        assert "retry-after" not in r.headers
    r = login("victim", BAD, IP)
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many attempts, try again later"
    assert 1 <= retry_after(r) <= 60
    assert urow("victim")["failed_attempts"] == 5


def test_correct_password_during_lock_is_429_and_counters_do_not_move(make_user, login):
    make_user("victim")
    for _ in range(5):
        login("victim", BAD, IP)
    before = urow("victim")
    r = login("victim", PW, IP)
    assert r.status_code == 429 and "access_token" not in r.text
    r2 = login("victim", BAD, IP)
    assert r2.status_code == 429
    after = urow("victim")
    assert after["failed_attempts"] == before["failed_attempts"] == 5
    assert after["locked_until"] == before["locked_until"]  # lock not extended


def test_escalation_1_2_4_minutes(make_user, login):
    make_user("victim")
    for _ in range(5):
        r = login("victim", BAD, IP)
    assert 1 <= retry_after(r) <= 60  # 1 minute
    expire_lock("victim")
    r = login("victim", BAD, IP)  # failure 6 -> 2 minutes
    assert r.status_code == 429 and 60 < retry_after(r) <= 120
    expire_lock("victim")
    r = login("victim", BAD, IP)  # failure 7 -> 4 minutes
    assert r.status_code == 429 and 120 < retry_after(r) <= 240
    expire_lock("victim")
    r = login("victim", BAD, IP)  # failure 8 -> 8 minutes
    assert r.status_code == 429 and 240 < retry_after(r) <= 480


def test_lock_is_capped_at_60_minutes(make_user, login):
    make_user("victim")
    pg_service.execute(
        "UPDATE users SET failed_attempts = 20, last_failed_at = now(), "
        "locked_until = now() - interval '1 second' WHERE username = 'victim'")
    r = login("victim", BAD, IP)
    assert r.status_code == 429
    assert 1800 < retry_after(r) <= 3600
    assert urow("victim")["failed_attempts"] == 21


def test_success_resets_counters(make_user, login):
    make_user("victim")
    for _ in range(3):
        login("victim", BAD, IP)
    assert urow("victim")["failed_attempts"] == 3
    assert login("victim", PW, IP).status_code == 200
    row = urow("victim")
    assert row["failed_attempts"] == 0 and row["locked_until"] is None and row["last_failed_at"] is None
    for _ in range(4):  # a fresh run of 4 does not lock
        assert login("victim", BAD, IP).status_code == 401
    assert pg_service.query_one(
        "SELECT last_login FROM users WHERE username = 'victim'")["last_login"] is not None


def test_quiet_window_resets_after_15_minutes(make_user, login):
    make_user("victim")
    pg_service.execute(
        "UPDATE users SET failed_attempts = 4, last_failed_at = now() - interval '20 minutes' "
        "WHERE username = 'victim'")
    assert login("victim", BAD, IP).status_code == 401  # would have been 5th
    assert urow("victim")["failed_attempts"] == 1


def test_quiet_window_counts_from_lock_expiry(make_user, login):
    """A lock that expired only 5 minutes ago must not let the offender shed
    the count, even though last_failed_at is 20 minutes old."""
    make_user("victim")
    pg_service.execute(
        "UPDATE users SET failed_attempts = 5, last_failed_at = now() - interval '20 minutes', "
        "locked_until = now() - interval '5 minutes' WHERE username = 'victim'")
    r = login("victim", BAD, IP)
    assert r.status_code == 429  # count 6 -> locked again
    assert urow("victim")["failed_attempts"] == 6

    # but once the expiry itself is 16 minutes old the count restarts
    pg_service.execute(
        "UPDATE users SET failed_attempts = 6, last_failed_at = now() - interval '30 minutes', "
        "locked_until = now() - interval '16 minutes' WHERE username = 'victim'")
    assert login("victim", BAD, IP).status_code == 401
    assert urow("victim")["failed_attempts"] == 1


# ---- uniform behaviour for unknown usernames ----------------------------

def test_unknown_username_matches_known_username_responses(make_user, login):
    make_user("realuser")

    def run(name):
        seen = []
        for _ in range(5):
            r = login(name, BAD, IP)
            seen.append((r.status_code, r.json(), "retry-after" in r.headers))
        extra = login(name, PW if name == "realuser" else "whatever", IP)
        seen.append((extra.status_code, extra.json(), "retry-after" in extra.headers))
        return seen

    known = run("realuser")
    unknown = run("ghostuser")
    assert known == unknown
    assert [s[0] for s in known] == [401, 401, 401, 401, 429, 429]


def test_unknown_username_is_keyed_per_username_and_ip(login):
    for _ in range(5):
        r = login("ghost", BAD, "198.51.100.1")
    assert r.status_code == 429
    # other IP, same username: independent counter
    assert login("ghost", BAD, "198.51.100.2").status_code == 401
    # other username, same IP: independent counter
    assert login("ghost2", BAD, "198.51.100.1").status_code == 401
    # case-insensitive key
    assert login("GHOST", BAD, "198.51.100.1").status_code == 429
    rows = pg_service.query(
        "SELECT username_lower, ip, failed_count FROM login_attempts ORDER BY username_lower, ip")
    assert {(r["username_lower"], r["ip"]) for r in rows} == {
        ("ghost", "198.51.100.1"), ("ghost", "198.51.100.2"), ("ghost2", "198.51.100.1")}


def test_unknown_username_escalates_like_known(login):
    for _ in range(5):
        login("ghost", BAD, IP)
    pg_service.execute("UPDATE login_attempts SET locked_until = now() - interval '1 second'")
    r = login("ghost", BAD, IP)
    assert r.status_code == 429 and 60 < retry_after(r) <= 120


def test_dummy_hash_verified_for_unknown_user(login, monkeypatch):
    import auth

    seen = []
    real = auth.verify_password

    def spy(plain, hashed):
        seen.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(auth, "verify_password", spy)
    assert login("nobody-here", BAD, IP).status_code == 401
    assert seen == [auth._DUMMY_HASH]


def test_dummy_hash_password_does_not_log_in_unknown_user(login):
    # the dummy hash's plaintext must never authenticate anyone
    assert login("nobody-here", "dummy-password-never-used", IP).status_code == 401


# ---- case-insensitive login, deactivated accounts ------------------------

def test_login_is_case_insensitive(make_user, login):
    make_user("LegacyUser")
    r = login("legacyuser", PW, IP)
    assert r.status_code == 200 and r.json()["user"]["username"] == "LegacyUser"
    assert login("LEGACYUSER", PW, IP).status_code == 200


def test_exact_case_match_wins_among_legacy_duplicates(make_user, login):
    # Simulate legacy case-variant rows (migration 011 skips the unique index
    # when they exist): drop the index for this test only.
    pg_service.execute("DROP INDEX IF EXISTS users_username_lower_uidx")
    try:
        make_user("Dup", password="Aaaa!1111Aaaa", email="dup-one@example.test")
        make_user("dup", password=PW, email="dup-two@example.test")
        assert login("dup", PW, IP).status_code == 200  # exact case wins
        assert login("Dup", "Aaaa!1111Aaaa", IP).status_code == 200
    finally:
        pg_service.execute("TRUNCATE users CASCADE")
        pg_service.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_uidx ON users (lower(username))")


def test_deactivated_user_403_only_after_correct_password(make_user, login):
    make_user("gone", is_active=False)
    r = login("gone", BAD, IP)
    assert r.status_code == 401 and r.json()["detail"] == "Incorrect username or password"
    r = login("gone", PW, IP)
    assert r.status_code == 403
    assert r.json()["detail"] == "Account deactivated. Contact your administrator."


# ---- concurrency ----------------------------------------------------------

def test_parallel_wrong_passwords_lose_no_updates(make_user, client):
    make_user("victim")

    def attempt(_):
        return client.post("/api/auth/login", json={"username": "victim", "password": BAD},
                           headers={"X-Real-IP": IP}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(attempt, range(8)))
    assert set(codes) <= {401, 429}
    row = urow("victim")
    counted = pg_service.query_one(
        "SELECT count(*) AS n FROM audit_logs WHERE action_type = 'LOGIN_FAILURE' "
        "AND details LIKE '%%reason=bad_credentials%%'")["n"]
    # every attempt that passed the lock check incremented exactly once
    assert row["failed_attempts"] == counted
    assert 5 <= row["failed_attempts"] <= 8
    assert row["locked_until"] is not None


# ---- T6: admin unlock -----------------------------------------------------

def test_admin_unlock_clears_lock(make_user, login, client, mint_token):
    admin_id = make_user("boss", role="admin")
    make_user("victim")
    for _ in range(5):
        login("victim", BAD, IP)
    login("victim-ghost", BAD, IP)  # unrelated attempt row stays
    pg_service.execute(
        "INSERT INTO login_attempts (username_lower, ip, failed_count) VALUES ('victim', %s, 2)", (IP,))
    assert login("victim", PW, IP).status_code == 429
    target_id = pg_service.query_one("SELECT id FROM users WHERE username = 'victim'")["id"]

    r = client.post(f"/api/admin/users/{target_id}/unlock", headers=bearer(mint_token(admin_id)))
    assert r.status_code == 200
    row = urow("victim")
    assert row["failed_attempts"] == 0 and row["locked_until"] is None and row["last_failed_at"] is None
    assert pg_service.query_one(
        "SELECT 1 AS x FROM login_attempts WHERE username_lower = 'victim'") is None
    assert login("victim", PW, IP).status_code == 200


def test_unlock_requires_admin(make_user, client, mint_token):
    mgr = make_user("mgr", role="manajer")
    target = make_user("victim")
    r = client.post(f"/api/admin/users/{target}/unlock", headers=bearer(mint_token(mgr)))
    assert r.status_code == 403
    assert client.post(f"/api/admin/users/{target}/unlock").status_code in (401, 403)


def test_unlock_cannot_target_self_or_equal_role(make_user, client, mint_token):
    admin_id = make_user("boss", role="admin")
    other_admin = make_user("boss2", role="admin")
    h = bearer(mint_token(admin_id))
    assert client.post(f"/api/admin/users/{admin_id}/unlock", headers=h).status_code == 400
    assert client.post(f"/api/admin/users/{other_admin}/unlock", headers=h).status_code == 403
    assert client.post("/api/admin/users/does-not-exist/unlock", headers=h).status_code == 404
