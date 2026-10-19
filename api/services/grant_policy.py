"""Access-grant policy helpers (migration 012).

One place for: the "active grant" predicate shared by every enforcement point,
ISO-Z timestamp formatting, LIKE escaping, audit-detail redaction of grant
reasons, grant event recording, the throttled last-accessed touch and expiry
resolution for the admin panel.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from services import pg_service

logger = logging.getLogger(__name__)

PERMISSIONS = ("full", "read")
EXPIRING_SOON_DAYS = 7
TOUCH_THROTTLE_MINUTES = 5
MAX_EXPIRY_YEARS = 5
MIN_EXPIRY_LEAD = timedelta(minutes=1)
EVENT_TYPES = (
    "granted", "revoked", "expired", "extended", "expiry_set", "expiry_cleared",
    "permission_changed", "visibility_changed", "reason_revealed",
)
ISO_Z_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
GRANT_ID_PATTERN = r"^[0-9a-fA-F-]{36}$"
_GRANT_ID_RE = re.compile(GRANT_ID_PATTERN)
_LEGACY_REASON_MARK = ", Alasan:"


def is_grant_id(value: str) -> bool:
    return bool(value) and _GRANT_ID_RE.match(value) is not None


def active_grant_sql(alias: str = "") -> str:
    """Single definition of an active grant: not revoked and not expired."""
    p = f"{alias}." if alias else ""
    return f"({p}revoked_at IS NULL AND ({p}expires_at IS NULL OR {p}expires_at >= NOW()))"


def iso_z(col: str) -> str:
    """SQL expression rendering a timestamptz column as ISO-8601 UTC with 'Z'."""
    return f"to_char({col} AT TIME ZONE 'UTC', '{ISO_Z_FORMAT}')"


def escape_like(value: str) -> str:
    """Escape LIKE wildcards; pair with ``ILIKE %s ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def redact_audit_details(action_type: Optional[str], details: Optional[str]) -> Optional[str]:
    """Strip the reason from legacy GRANT_ACCESS audit rows at read time."""
    if details and action_type == "GRANT_ACCESS":
        idx = details.find(_LEGACY_REASON_MARK)
        if idx >= 0:
            return details[:idx]
    return details


def AUDIT_DETAILS_SEARCH_SQL(alias: str = "") -> str:
    """Search expression over audit details that never matches a legacy reason."""
    p = f"{alias}." if alias else ""
    return (f"(CASE WHEN {p}action_type = 'GRANT_ACCESS' "
            f"THEN split_part({p}details, ', Alasan:', 1) ELSE {p}details END)")


def record_event(conn, grant: dict, event_type: str, actor: Optional[dict],
                 old: Optional[str] = None, new: Optional[str] = None,
                 when=None) -> None:
    """Insert an access_grant_events row on the caller's connection/transaction.

    ``grant`` must carry id, doc_id, granted_to, grantee_name and judul (the
    locked/joined row). Never put reason text in old/new.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type}")
    conn.execute(
        "INSERT INTO access_grant_events "
        "(grant_id, event_type, actor_id, actor_name, granted_to, grantee_name, "
        " doc_id, doc_title, old_value, new_value, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))",
        (grant["id"], event_type,
         actor.get("id") if actor else None,
         actor.get("username") if actor else None,
         grant.get("granted_to"), grant.get("grantee_name"),
         grant.get("doc_id"), grant.get("judul"), old, new, when))


def touch_grants(user_id: str, doc_ids) -> None:
    """Set last_accessed_at (at most once per TOUCH_THROTTLE_MINUTES); never raises."""
    ids = [str(d) for d in doc_ids if d]
    if not user_id or not ids:
        return
    try:
        pg_service.execute(
            "UPDATE access_grants SET last_accessed_at = NOW() "
            "WHERE granted_to = %s AND doc_id = ANY(%s) AND "
            + active_grant_sql() +
            " AND (last_accessed_at IS NULL "
            "      OR last_accessed_at < NOW() - make_interval(mins => %s))",
            (user_id, ids, TOUCH_THROTTLE_MINUTES))
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("touch_grants failed: %s", e)


def resolve_expiry(spec: dict, current_expires_at: Optional[datetime],
                   now: Optional[datetime] = None) -> Optional[datetime]:
    """Return the new expires_at (None = never) for an ExpirySpec dict.

    Raises ValueError with an Indonesian message on invalid input.
    """
    now = now or datetime.now(timezone.utc)
    mode = spec.get("mode")
    if mode == "never":
        return None
    if mode == "extend":
        days = spec.get("days")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("Jumlah hari harus antara 1 dan 365.")
        base = now
        if current_expires_at is not None and current_expires_at > now:
            base = current_expires_at
        return base + timedelta(days=days)
    if mode == "set":
        value = spec.get("expires_at")
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Format tanggal tidak valid.")
        if not isinstance(value, datetime):
            raise ValueError("Tanggal kedaluwarsa wajib diisi.")
        if value.tzinfo is None:
            raise ValueError("Tanggal harus menyertakan zona waktu.")
        if value <= now + MIN_EXPIRY_LEAD:
            raise ValueError("Tanggal kedaluwarsa harus di masa depan.")
        if value > now + timedelta(days=365 * MAX_EXPIRY_YEARS + 2):
            raise ValueError(f"Tanggal kedaluwarsa maksimal {MAX_EXPIRY_YEARS} tahun dari sekarang.")
        return value
    raise ValueError("Mode kedaluwarsa tidak valid.")
