"""T7: sliding-window limiter (fake clock, no DB) + the 429 contract on a
real endpoint (DB)."""
import pytest
from conftest import bearer

from services.rate_limit import SlidingWindowLimiter, get_limit


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(limit=6):
    clock = FakeClock()
    return SlidingWindowLimiter(limit, 60.0, clock), clock


def test_six_allowed_seventh_denied_with_retry_after():
    lim, clock = make()
    for _ in range(6):
        assert lim.check("analyze:u1") == (True, 0)
    clock.t += 10
    allowed, retry = lim.check("analyze:u1")
    assert allowed is False
    assert retry == 50  # oldest hit leaves the window 60 s after it was made


def test_denied_request_is_not_recorded():
    lim, clock = make()
    for _ in range(6):
        lim.check("k")
    for _ in range(20):
        assert lim.check("k")[0] is False
    clock.t += 60.1
    assert lim.check("k") == (True, 0)  # no penalty accumulated


def test_window_slides_one_slot_at_a_time():
    lim, clock = make()
    lim.check("k")            # t=1000
    clock.t += 30
    for _ in range(5):
        lim.check("k")        # t=1030
    assert lim.check("k")[0] is False
    clock.t += 30.1           # t=1060.1: only the first hit expired
    assert lim.check("k")[0] is True
    assert lim.check("k")[0] is False


def test_keys_are_isolated():
    lim, _ = make()
    for _ in range(6):
        lim.check("analyze:u1")
    assert lim.check("analyze:u1")[0] is False
    assert lim.check("analyze:u2")[0] is True          # other user
    assert lim.check("analyze_pasals:u1")[0] is True   # other bucket


def test_retry_after_is_at_least_one_second():
    lim, clock = make(1)
    lim.check("k")
    clock.t += 59.9999
    assert lim.check("k") == (False, 1)


def test_idle_keys_are_purged():
    lim, clock = make()
    for i in range(50):
        lim.check(f"k{i}")
    assert len(lim) == 50
    clock.t += 200
    lim.check("fresh")  # triggers the sweep
    assert len(lim) == 1


def test_limit_env(monkeypatch):
    monkeypatch.delenv("LLM_RATE_PER_MIN", raising=False)
    assert get_limit() == 6
    monkeypatch.setenv("LLM_RATE_PER_MIN", "2")
    assert get_limit() == 2
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv("LLM_RATE_PER_MIN", bad)
        assert get_limit() == 6
    # callable limit is read on every check
    monkeypatch.setenv("LLM_RATE_PER_MIN", "1")
    lim = SlidingWindowLimiter()
    assert lim.check("k")[0] is True
    assert lim.check("k")[0] is False


# ---- over HTTP ------------------------------------------------------------

PAYLOAD = {"reg_id": "999999", "nomor": "N-1", "judul": "J"}


@pytest.mark.db
def test_seventh_request_in_a_minute_is_429_with_retry_after(make_user, client, mint_token):
    uid = make_user("alice")
    h = bearer(mint_token(uid))
    for i in range(6):
        assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 200, i
    r = client.post("/api/analyze", json=PAYLOAD, headers=h)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert r.json()["detail"] == "Too many requests, try again later"


@pytest.mark.db
def test_buckets_and_users_are_independent_over_http(make_user, client, mint_token):
    a = bearer(mint_token(make_user("alice")))
    b = bearer(mint_token(make_user("bob")))
    for _ in range(6):
        client.post("/api/analyze", json=PAYLOAD, headers=a)
    assert client.post("/api/analyze", json=PAYLOAD, headers=a).status_code == 429
    # same user, other endpoint bucket
    assert client.post("/api/analyze-pasals", json=PAYLOAD, headers=a).status_code == 200
    # other user, same endpoint
    assert client.post("/api/analyze", json=PAYLOAD, headers=b).status_code == 200


@pytest.mark.db
def test_chat_is_limited(make_user, client, mint_token):
    h = bearer(mint_token(make_user("alice")))
    # empty messages -> 400 from the handler, after the limiter ran
    for _ in range(6):
        assert client.post("/api/chat", json={"messages": []}, headers=h).status_code == 400
    r = client.post("/api/chat", json={"messages": []}, headers=h)
    assert r.status_code == 429 and "Retry-After" in r.headers


@pytest.mark.db
def test_forbidden_role_does_not_consume_budget(make_user, client, mint_token):
    h = bearer(mint_token(make_user("eng", role="insinyur ti")))
    for _ in range(10):
        assert client.post("/api/knowledge-graph/analyze-scenario",
                           json={"scenario": "x"}, headers=h).status_code == 403
    # the same account still has its full /analyze budget
    for _ in range(6):
        assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 200


@pytest.mark.db
def test_limit_follows_env(make_user, client, mint_token, monkeypatch):
    monkeypatch.setenv("LLM_RATE_PER_MIN", "2")
    h = bearer(mint_token(make_user("alice")))
    assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 200
    assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 200
    assert client.post("/api/analyze", json=PAYLOAD, headers=h).status_code == 429
