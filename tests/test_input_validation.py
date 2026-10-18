"""T2: input limits on login / register / change-password (needs DB because
auth.py seeds templates at import)."""
import pytest
from psycopg import errors as pg_errors

pytestmark = pytest.mark.db

GOOD_PW = "Str0ng!Passw0rd"


def reg(client, **over):
    body = {"username": "newuser", "email": "newuser@example.test", "password": GOOD_PW}
    body.update(over)
    return client.post("/api/auth/register", json=body)


# ---- login model ---------------------------------------------------------

def test_login_rejects_empty_and_oversized(client):
    post = lambda u, p: client.post("/api/auth/login", json={"username": u, "password": p})
    assert post("", "x").status_code == 422
    assert post("bob", "").status_code == 422
    assert post("u" * 65, "x").status_code == 422
    assert post("u" * 64, "x").status_code in (401, 429)  # accepted by the model
    assert post("bob", "p" * 73).status_code == 422
    # 36 two-byte characters = 36 chars but 72 bytes (accepted); 37 = 74 bytes
    assert post("bob", "é" * 36).status_code == 401
    assert post("bob", "é" * 37).status_code == 422
    # exactly 72 ASCII bytes accepted
    assert post("bob", "p" * 72).status_code == 401
    # NUL cannot be stored in PostgreSQL text: clean 422, not a 500
    assert post("bo\x00b", "x").status_code == 422


def test_short_legacy_password_can_still_log_in(client, make_user):
    make_user("legacy", password="A1!")
    r = client.post("/api/auth/login", json={"username": "legacy", "password": "A1!"})
    assert r.status_code == 200
    assert r.json()["access_token"]


# ---- register model ------------------------------------------------------

@pytest.mark.parametrize("username", [
    "ab", "a" * 65, "has space", "-leading", ".leading", "uni\u00e7ode", "semi;colon",
    "new\nline", "\u0430dmin",  # Cyrillic a look-alike
])
def test_register_bad_usernames(client, username):
    assert reg(client, username=username).status_code == 422


@pytest.mark.parametrize("username", ["abc", "demoacc", "smoke_kg", "a.b-c_d", "A" + "b" * 63])
def test_register_good_usernames(client, username):
    assert reg(client, username=username, email=f"{username.lower()}@example.test").status_code == 200


def test_register_email_and_password_limits(client):
    assert reg(client, email="a" * 250 + "@x.io").status_code == 422  # >254
    assert reg(client, password="Sh0rt!pw").status_code == 422  # <10
    assert reg(client, password="Aa1!" + "x" * 69).status_code == 422  # 73 bytes
    assert reg(client, password="alllowercase1!").status_code == 422  # no uppercase
    assert reg(client, password="NoNumbers!!!!").status_code == 422
    assert reg(client, password="NoSymbols12345").status_code == 422
    # 36 x 2-byte chars + complexity: >72 bytes though <72 chars
    assert reg(client, password="Aa1!" + "é" * 35).status_code == 422
    assert reg(client, password="Aa1!" + "x" * 68).status_code == 200  # exactly 72 bytes


def test_register_error_messages_for_length(client):
    r = reg(client, password="Sh0rt!pw")
    assert "at least 10 characters" in r.text
    r = reg(client, password="Aa1!" + "x" * 69)
    assert "at most 72 bytes" in r.text


def test_register_valid_payload(client):
    r = reg(client)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["username"] == "newuser" and body["user"]["role"] == "pengguna"


def test_register_duplicates_are_case_insensitive(client, make_user):
    make_user("Admin", email="boss@example.test")
    r = reg(client, username="admin", email="other@example.test")
    assert r.status_code == 400 and r.json()["detail"] == "Username already registered"
    r = reg(client, username="someone", email="BOSS@example.test")
    assert r.status_code == 400 and r.json()["detail"] == "Email already registered"


def test_register_unique_violation_race_is_a_clean_400(client, monkeypatch):
    """Both checks pass, then the INSERT loses the race: 400, not 500."""
    from services import pg_service

    real_execute = pg_service.execute

    class Diag:
        constraint_name = "users_username_lower_uidx"

    def boom(sql, params=None):
        if sql.startswith("INSERT INTO users"):
            raise _with_diag(Diag.constraint_name)
        return real_execute(sql, params)

    monkeypatch.setattr(pg_service, "execute", boom)
    r = reg(client)
    assert r.status_code == 400 and r.json()["detail"] == "Username already registered"

    Diag.constraint_name = "users_email_lower_uidx"
    r = reg(client)
    assert r.status_code == 400 and r.json()["detail"] == "Email already registered"


def _with_diag(constraint):
    """psycopg errors expose ``diag`` read-only; subclass to fake it."""
    class _Diag:
        constraint_name = constraint

    class _E(pg_errors.UniqueViolation):
        @property
        def diag(self):
            return _Diag()

    return _E("duplicate")


def test_real_unique_index_race_end_to_end(client, make_user):
    """No monkeypatching: bypass the pre-checks by inserting a case-variant
    row directly; the unique index (migration 011) must fire."""
    from services import pg_service

    make_user("Racer", email="racer@example.test")
    with pytest.raises(pg_errors.UniqueViolation):
        pg_service.execute(
            "INSERT INTO users (id, username, password_hash, email) VALUES ('x','racer','h','z@example.test')")


# ---- change-password model ----------------------------------------------

def test_change_password_limits(client, make_user, mint_token):
    from conftest import bearer

    uid = make_user("changer")
    h = bearer(mint_token(uid))
    post = lambda old, new: client.post("/api/auth/change-password", headers=h,
                                        json={"old_password": old, "new_password": new})
    assert post("", GOOD_PW + "x").status_code == 422
    assert post("p" * 73, GOOD_PW + "x").status_code == 422
    assert post(GOOD_PW, "Sh0rt!pw").status_code == 422
    assert post(GOOD_PW, "Aa1!" + "x" * 69).status_code == 422


# ---- bcrypt 5.0.0 behaviour for >72-byte input ---------------------------

def test_bcrypt_over_72_bytes_behaviour_is_documented_and_never_raises_from_verify():
    """bcrypt 5.0.0 raises ValueError from hashpw() for passwords longer than
    72 bytes (older versions silently truncated). checkpw() is expected to
    behave the same. This test records the actual outcome on the installed
    version and asserts auth.verify_password swallows it, so no endpoint can
    500 on oversized input even if the pydantic 72-byte cap were bypassed."""
    import bcrypt

    import auth

    long_pw = b"x" * 100
    hash_ok = bcrypt.hashpw(b"short", bcrypt.gensalt()).decode()
    try:
        bcrypt.hashpw(long_pw, bcrypt.gensalt())
        hashpw_raises = False
    except ValueError:
        hashpw_raises = True
    try:
        bcrypt.checkpw(long_pw, hash_ok.encode())
        checkpw_raises = False
    except ValueError:
        checkpw_raises = True
    # Observed with bcrypt==5.0.0 (see the assertion below, which pins it):
    assert hashpw_raises is True
    assert checkpw_raises is True
    assert auth.verify_password("x" * 100, hash_ok) is False
    assert auth.verify_password("short", "not-a-bcrypt-hash") is False
