"""T8: notification SSE hardening (D4): auth, own-id only, 3-stream cap,
disconnect handling, POST route removed."""
import asyncio

import pytest
from conftest import bearer
from fastapi import HTTPException

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _reset_streams():
    from routers import notification

    notification._open_streams.clear()
    yield
    notification._open_streams.clear()


class FakeRequest:
    """Just enough of starlette.Request for the stream generator."""

    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def user(uid):
    return {"id": uid, "username": "u", "email": "u@example.test", "role": "pengguna"}


def test_missing_token_rejected(client):
    assert client.get("/api/notifications/abc").status_code in (401, 403)


def test_other_users_id_is_403(make_user, mint_token, client):
    me = make_user("alice")
    other = make_user("bob")
    r = client.get(f"/api/notifications/{other}", headers=bearer(mint_token(me)))
    assert r.status_code == 403


def test_admin_has_no_override(make_user, mint_token, client):
    admin = make_user("root", role="admin")
    other = make_user("bob")
    assert client.get(f"/api/notifications/{other}", headers=bearer(mint_token(admin))).status_code == 403


def test_post_route_is_removed(make_user, mint_token, client):
    uid = make_user("alice")
    body = {"jobId": "j", "status": "done", "message": "m"}
    # 405 (path exists for GET, POST gone) with and without a token
    assert client.post(f"/api/notifications/{uid}", json=body).status_code == 405
    assert client.post(f"/api/notifications/{uid}", json=body,
                       headers=bearer(mint_token(uid))).status_code == 405


def test_own_stream_yields_keepalive_first_chunk():
    from routers import notification

    async def go():
        req = FakeRequest()
        resp = await notification.stream_notifications("u1", req, user("u1"))
        assert resp.media_type == "text/event-stream"
        first = await resp.body_iterator.__anext__()
        assert first == "data: keep-alive\n\n"
        await resp.body_iterator.aclose()

    asyncio.run(go())


def test_fourth_concurrent_stream_is_429_and_slot_frees_on_close():
    from routers import notification

    async def go():
        streams = []
        for _ in range(3):
            resp = await notification.stream_notifications("u1", FakeRequest(), user("u1"))
            await resp.body_iterator.__anext__()   # start it (slot counted)
            streams.append(resp)
        with pytest.raises(HTTPException) as exc:
            await notification.stream_notifications("u1", FakeRequest(), user("u1"))
        assert exc.value.status_code == 429
        assert "Retry-After" in exc.value.headers
        # another user is unaffected
        other = await notification.stream_notifications("u2", FakeRequest(), user("u2"))
        await other.body_iterator.__anext__()
        await other.body_iterator.aclose()
        # closing one stream frees a slot
        await streams[0].body_iterator.aclose()
        again = await notification.stream_notifications("u1", FakeRequest(), user("u1"))
        await again.body_iterator.__anext__()
        for s in streams[1:] + [again]:
            await s.body_iterator.aclose()
        assert notification._open_streams == {}

    asyncio.run(go())


def test_stream_stops_and_releases_slot_on_disconnect():
    from routers import notification

    async def go():
        req = FakeRequest()
        resp = await notification.stream_notifications("u1", req, user("u1"))
        it = resp.body_iterator
        assert await it.__anext__() == "data: keep-alive\n\n"
        assert notification._open_streams == {"u1": 1}
        req.disconnected = True
        # patch the sleep so the loop re-checks immediately
        orig = asyncio.sleep

        async def fast(_):
            await orig(0)

        notification.asyncio.sleep = fast
        try:
            with pytest.raises(StopAsyncIteration):
                await it.__anext__()
        finally:
            notification.asyncio.sleep = orig
        assert notification._open_streams == {}

    asyncio.run(go())


def test_over_http_the_cap_applies_before_streaming(make_user, mint_token, client):
    """429 is returned as a normal response (nothing starts streaming), so the
    TestClient can assert it even though real streams never end."""
    from routers import notification

    uid = make_user("alice")
    notification._open_streams[uid] = notification.MAX_STREAMS_PER_USER
    r = client.get(f"/api/notifications/{uid}", headers=bearer(mint_token(uid)))
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "30"
