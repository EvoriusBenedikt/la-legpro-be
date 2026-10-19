"""grant_policy helpers (migration 012)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from services import grant_policy as gp
from services import pg_service

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_escape_like_escapes_wildcards_and_backslash():
    assert gp.escape_like(r"50%_a\b") == r"50\%\_a\\b"


def test_redact_legacy_grant_access_reason():
    legacy = "Diberikan kepada: u1, Dokumen: Judul, Alasan: rahasia sekali"
    assert gp.redact_audit_details("GRANT_ACCESS", legacy) == "Diberikan kepada: u1, Dokumen: Judul"
    new = "Diberikan kepada: u1, Dokumen: Judul"
    assert gp.redact_audit_details("GRANT_ACCESS", new) == new
    # other action types are untouched even if they contain the marker
    assert gp.redact_audit_details("OTHER", legacy) == legacy
    assert gp.redact_audit_details("GRANT_ACCESS", None) is None


def test_is_grant_id():
    assert gp.is_grant_id(str(uuid.uuid4()))
    assert not gp.is_grant_id("x" * 36)
    assert not gp.is_grant_id("abc")
    assert not gp.is_grant_id("")


def test_active_predicate_aliasing():
    assert gp.active_grant_sql("ag").startswith("(ag.revoked_at IS NULL")
    assert "revoked_at IS NULL" in gp.active_grant_sql()


def test_resolve_expiry_never_and_extend():
    assert gp.resolve_expiry({"mode": "never"}, NOW) is None
    assert gp.resolve_expiry({"mode": "extend", "days": 7}, None, now=NOW) == NOW + timedelta(days=7)
    future = NOW + timedelta(days=10)
    assert gp.resolve_expiry({"mode": "extend", "days": 7}, future, now=NOW) == future + timedelta(days=7)
    # expired grants restart from now
    past = NOW - timedelta(days=3)
    assert gp.resolve_expiry({"mode": "extend", "days": 30}, past, now=NOW) == NOW + timedelta(days=30)


@pytest.mark.parametrize("spec", [
    {"mode": "extend"}, {"mode": "extend", "days": 0}, {"mode": "extend", "days": 366},
    {"mode": "extend", "days": True}, {"mode": "bogus"},
    {"mode": "set"}, {"mode": "set", "expires_at": "not a date"},
    {"mode": "set", "expires_at": "2030-06-01T00:00:00"},            # naive
    {"mode": "set", "expires_at": "2029-12-31T00:00:00Z"},           # past
    {"mode": "set", "expires_at": "2030-01-01T12:00:30Z"},           # < now + 1 min
    {"mode": "set", "expires_at": "2040-01-01T00:00:00Z"},           # > 5 years
])
def test_resolve_expiry_rejects_invalid(spec):
    with pytest.raises(ValueError):
        gp.resolve_expiry(spec, None, now=NOW)


def test_resolve_expiry_set_ok():
    got = gp.resolve_expiry({"mode": "set", "expires_at": "2030-02-01T00:00:00Z"}, None, now=NOW)
    assert got == datetime(2030, 2, 1, tzinfo=timezone.utc)


@pytest.mark.db
def test_touch_grants_throttles_to_one_write(make_user):
    uid = make_user("toucher")
    gid = str(uuid.uuid4())
    pg_service.execute(
        "INSERT INTO access_grants (id, doc_id, granted_by, granted_to, reason) "
        "VALUES (%s, '1', %s, %s, 'alasan uji')", (gid, uid, uid))
    gp.touch_grants(uid, ["1"])
    first = pg_service.query_one("SELECT last_accessed_at FROM access_grants WHERE id = %s", (gid,))
    assert first["last_accessed_at"] is not None
    gp.touch_grants(uid, ["1"])
    second = pg_service.query_one("SELECT last_accessed_at FROM access_grants WHERE id = %s", (gid,))
    assert second["last_accessed_at"] == first["last_accessed_at"]
    # older than the throttle window -> written again
    pg_service.execute(
        "UPDATE access_grants SET last_accessed_at = NOW() - interval '10 minutes' WHERE id = %s", (gid,))
    gp.touch_grants(uid, ["1"])
    third = pg_service.query_one(
        "SELECT last_accessed_at > NOW() - interval '1 minute' AS fresh FROM access_grants WHERE id = %s", (gid,))
    assert third["fresh"] is True
    # revoked grants are never touched
    pg_service.execute(
        "UPDATE access_grants SET revoked_at = NOW(), last_accessed_at = NULL WHERE id = %s", (gid,))
    gp.touch_grants(uid, ["1"])
    assert pg_service.query_one(
        "SELECT last_accessed_at FROM access_grants WHERE id = %s", (gid,))["last_accessed_at"] is None
