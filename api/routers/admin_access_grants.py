"""Admin access-grants panel API (migration 012).

List/search/filter/sort, history, single and bulk mutations (revoke, expiry,
permission, reason visibility) and the audited reason reveal. Every endpoint
requires the admin role; SQL is parameterized; sort/enum values are
whitelisted. Hidden reason text is only returned by POST .../reveal-reason.
"""
import logging
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator

import auth
from services import grant_policy as gp
from services import pg_service
from services.db_service import log_audit

router = APIRouter(prefix="/api/admin/access-grants", tags=["admin-access-grants"])
logger = logging.getLogger(__name__)

IDS_CAP = 1000
BULK_MAX = 100
GrantId = Path(..., pattern=gp.GRANT_ID_PATTERN)

FROM_SQL = (
    "FROM access_grants ag "
    "JOIN users recipient ON recipient.id = ag.granted_to "
    "LEFT JOIN users granter ON granter.id = ag.granted_by "
    "LEFT JOIN regulations r ON ag.doc_id = r.id::text ")

SORTS = {
    "newest": "ag.created_at DESC, ag.id",
    "expiry": "ag.expires_at ASC NULLS LAST, ag.id",
    "last_accessed": "ag.last_accessed_at DESC NULLS LAST, ag.id",
    "username": "LOWER(recipient.username) ASC, ag.id",
}


# ── Models ──────────────────────────────────────────────────────────────────

class ExpirySpec(BaseModel):
    mode: Literal["extend", "set", "never"]
    days: Optional[int] = Field(None, ge=1, le=365)
    expires_at: Optional[str] = Field(None, max_length=40)


class RevokeBody(BaseModel):
    note: Optional[str] = Field(None, max_length=200)


class PermissionBody(BaseModel):
    permission: Literal["full", "read"]


class VisibilityBody(BaseModel):
    reason_visible: bool


class BulkBody(BaseModel):
    action: Literal["revoke", "extend", "reason_visibility"]
    ids: List[str] = Field(..., min_length=1, max_length=BULK_MAX)
    expiry: Optional[ExpirySpec] = None
    reason_visible: Optional[bool] = None

    @field_validator("ids")
    @classmethod
    def _ids_valid(cls, v):
        for item in v:
            if not gp.is_grant_id(item):
                raise ValueError("Format id tidak valid.")
        return v


class GrantError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


# ── Helpers ─────────────────────────────────────────────────────────────────

def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filters(status, search, permission, expiring, never_expires, granted_by):
    """Shared WHERE for list and /ids. Returns (where_sql, params)."""
    clauses = ["recipient.is_active = TRUE"]
    params: list = []
    if status == "expired":
        clauses.append("(ag.revoked_at IS NULL AND ag.expires_at IS NOT NULL AND ag.expires_at < NOW())")
        if expiring or never_expires:
            clauses.append("FALSE")
    else:
        clauses.append(gp.active_grant_sql("ag"))
    if search:
        like = f"%{gp.escape_like(search.strip())}%"
        clauses.append(
            "(recipient.username ILIKE %s ESCAPE '\\' OR recipient.email ILIKE %s ESCAPE '\\' "
            "OR r.judul ILIKE %s ESCAPE '\\')")
        params.extend([like, like, like])
    if permission:
        clauses.append("ag.permission = %s")
        params.append(permission)
    if expiring:
        clauses.append(
            "(ag.expires_at IS NOT NULL AND ag.expires_at >= NOW() "
            f"AND ag.expires_at < NOW() + interval '{int(gp.EXPIRING_SOON_DAYS)} days')")
    if never_expires:
        clauses.append("ag.expires_at IS NULL")
    if granted_by:
        clauses.append("ag.granted_by = %s")
        params.append(granted_by)
    return "WHERE " + " AND ".join(clauses) + " ", params


def _lock_grant(conn, grant_id: str) -> dict:
    row = conn.execute(
        "SELECT ag.*, ru.username AS grantee_name, gb.username AS granter_name, r.judul "
        "FROM access_grants ag "
        "LEFT JOIN users ru ON ru.id = ag.granted_to "
        "LEFT JOIN users gb ON gb.id = ag.granted_by "
        "LEFT JOIN regulations r ON ag.doc_id = r.id::text "
        "WHERE ag.id = %s FOR UPDATE OF ag", (grant_id,)).fetchone()
    if not row:
        raise GrantError(404, "not_found", "Grant tidak ditemukan.")
    return row


def _require_open(g: dict) -> None:
    if g["revoked_at"] is not None:
        raise GrantError(409, "already_revoked", "Akses sudah dicabut.")


def _audit_text(g: dict, change: str) -> str:
    return f"grant={g['id']} user={g.get('grantee_name') or g.get('granted_to')} doc={g.get('doc_id')}; {change}"


def _http(err: GrantError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.detail)


def _do_revoke(conn, g: dict, actor: dict, note: Optional[str]) -> tuple:
    _require_open(g)
    conn.execute("UPDATE access_grants SET revoked_at = NOW(), revoked_by = %s WHERE id = %s",
                 (actor["id"], g["id"]))
    old = _iso(g["expires_at"]) or "none"
    gp.record_event(conn, g, "revoked", actor, old=old, new=(note or None))
    details = _audit_text(g, "revoked")
    if note:
        details += f"; note={note}"
    return ("REVOKE_ACCESS", details)


def _do_expiry(conn, g: dict, actor: dict, spec: dict) -> tuple:
    _require_open(g)
    try:
        new_exp = gp.resolve_expiry(spec, g["expires_at"])
    except ValueError as e:
        raise GrantError(400, "invalid_expiry", str(e))
    conn.execute("UPDATE access_grants SET expires_at = %s, expired_logged_at = NULL WHERE id = %s",
                 (new_exp, g["id"]))
    mode = spec.get("mode")
    event = {"extend": "extended", "set": "expiry_set", "never": "expiry_cleared"}[mode]
    old, new = _iso(g["expires_at"]) or "none", _iso(new_exp) or "none"
    gp.record_event(conn, g, event, actor, old=old, new=new)
    return ("UPDATE_ACCESS_EXPIRY", _audit_text(g, f"expires_at: {old} -> {new}"))


def _do_visibility(conn, g: dict, actor: dict, visible: bool) -> Optional[tuple]:
    _require_open(g)
    if bool(g["reason_visible"]) == visible:
        return None
    conn.execute("UPDATE access_grants SET reason_visible = %s WHERE id = %s", (visible, g["id"]))
    old, new = str(bool(g["reason_visible"])).lower(), str(visible).lower()
    gp.record_event(conn, g, "visibility_changed", actor, old=old, new=new)
    return ("UPDATE_ACCESS_REASON_VISIBILITY", _audit_text(g, f"reason_visible: {old} -> {new}"))


def _flush_audit(actor: dict, items: list) -> None:
    for g_id, entry in items:
        if entry:
            log_audit(actor["id"], entry[0], g_id, entry[1])


def _run_single(grant_id: str, actor: dict, fn):
    """Lock the grant, apply fn(conn, grant) in one transaction, audit after commit."""
    try:
        with pg_service.get_conn() as conn:
            g = _lock_grant(conn, grant_id)
            entry = fn(conn, g)
    except GrantError as e:
        raise _http(e)
    except HTTPException:
        raise
    except Exception:
        logger.exception("access grant mutation failed")
        raise HTTPException(status_code=500, detail="Internal server error")
    _flush_audit(actor, [(grant_id, entry)])
    return g, entry


# ── Read endpoints ──────────────────────────────────────────────────────────

@router.get("")
async def list_grants(
    status: Literal["active", "expired"] = Query("active"),
    search: Optional[str] = Query(None, max_length=100),
    permission: Optional[Literal["full", "read"]] = Query(None),
    expiring: bool = Query(False),
    never_expires: bool = Query(False),
    granted_by: Optional[str] = Query(None, max_length=64),
    sort: Literal["newest", "expiry", "last_accessed", "username"] = Query("newest"),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=100),
    current_user: dict = Depends(auth.require_role("admin")),
):
    where_sql, params = _filters(status, search, permission, expiring, never_expires, granted_by)
    total_row = pg_service.query_one(f"SELECT COUNT(*) AS n {FROM_SQL}{where_sql}", params)
    total = total_row["n"] if total_row else 0
    soon = int(gp.EXPIRING_SOON_DAYS)
    rows = pg_service.query(
        "SELECT ag.id, ag.doc_id, ag.granted_to, recipient.username, recipient.email, "
        "ag.granted_by, COALESCE(granter.username, '(akun dihapus)') AS granted_by_username, "
        "CASE WHEN ag.reason_visible THEN ag.reason ELSE NULL END AS reason, "
        "NOT ag.reason_visible AS reason_hidden, "
        f"{gp.iso_z('ag.expires_at')} AS expires_at, "
        f"{gp.iso_z('ag.created_at')} AS created_at, "
        f"{gp.iso_z('ag.last_accessed_at')} AS last_accessed_at, "
        "ag.permission, r.judul, r.klasifikasi, "
        "(ag.expires_at IS NOT NULL AND ag.expires_at >= NOW() "
        f" AND ag.expires_at < NOW() + interval '{soon} days') AS expiring_soon, "
        "CASE WHEN ag.expires_at IS NOT NULL AND ag.expires_at < NOW() "
        "     THEN 'expired' ELSE 'active' END AS status "
        f"{FROM_SQL}{where_sql}ORDER BY {SORTS[sort]} LIMIT %s OFFSET %s",
        params + [page_size, (page - 1) * page_size])
    active_row = pg_service.query_one(
        f"SELECT COUNT(*) AS n {FROM_SQL}WHERE recipient.is_active = TRUE AND {gp.active_grant_sql('ag')}")
    granters = pg_service.query(
        "SELECT DISTINCT gb.id, gb.username FROM access_grants ag "
        "JOIN users gb ON gb.id = ag.granted_by "
        "WHERE ag.revoked_at IS NULL ORDER BY gb.username LIMIT 200")
    return {
        "grants": rows, "total": total, "page": page, "page_size": page_size,
        "active_total": active_row["n"] if active_row else 0,
        "expiring_soon_days": soon, "granters": granters,
    }


@router.get("/ids")
async def list_grant_ids(
    status: Literal["active", "expired"] = Query("active"),
    search: Optional[str] = Query(None, max_length=100),
    permission: Optional[Literal["full", "read"]] = Query(None),
    expiring: bool = Query(False),
    never_expires: bool = Query(False),
    granted_by: Optional[str] = Query(None, max_length=64),
    sort: Literal["newest", "expiry", "last_accessed", "username"] = Query("newest"),
    current_user: dict = Depends(auth.require_role("admin")),
):
    where_sql, params = _filters(status, search, permission, expiring, never_expires, granted_by)
    total_row = pg_service.query_one(f"SELECT COUNT(*) AS n {FROM_SQL}{where_sql}", params)
    total = total_row["n"] if total_row else 0
    rows = pg_service.query(
        f"SELECT ag.id {FROM_SQL}{where_sql}ORDER BY {SORTS[sort]} LIMIT %s",
        params + [IDS_CAP])
    return {"ids": [r["id"] for r in rows], "total": total, "truncated": total > IDS_CAP}


def _parse_day(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must use the YYYY-MM-DD format")
    return value


@router.get("/history")
async def grant_history(
    search: Optional[str] = Query(None, max_length=100),
    event_type: Optional[str] = Query(None, pattern="^(" + "|".join(gp.EVENT_TYPES) + ")$"),
    date_from: Optional[str] = Query(None, max_length=10),
    date_to: Optional[str] = Query(None, max_length=10),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=100),
    current_user: dict = Depends(auth.require_role("admin")),
):
    clauses = ["TRUE"]
    params: list = []
    if event_type:
        clauses.append("e.event_type = %s")
        params.append(event_type)
    else:
        clauses.append("e.event_type <> 'reason_revealed'")
    if search:
        like = f"%{gp.escape_like(search.strip())}%"
        clauses.append("(e.grantee_name ILIKE %s ESCAPE '\\' OR e.doc_title ILIKE %s ESCAPE '\\' "
                       "OR e.actor_name ILIKE %s ESCAPE '\\')")
        params.extend([like, like, like])
    d_from, d_to = _parse_day(date_from), _parse_day(date_to)
    if d_from:
        clauses.append("e.created_at >= %s::date")
        params.append(d_from)
    if d_to:
        clauses.append("e.created_at < (%s::date + INTERVAL '1 day')")
        params.append(d_to)
    where_sql = "WHERE " + " AND ".join(clauses) + " "
    total_row = pg_service.query_one(f"SELECT COUNT(*) AS n FROM access_grant_events e {where_sql}", params)
    events = pg_service.query(
        "SELECT e.id, e.grant_id, e.event_type, e.actor_name, e.grantee_name, e.doc_title, "
        f"e.doc_id, e.old_value, e.new_value, {gp.iso_z('e.created_at')} AS created_at "
        f"FROM access_grant_events e {where_sql}ORDER BY e.id DESC LIMIT %s OFFSET %s",
        params + [page_size, (page - 1) * page_size])
    return {"events": events, "total": total_row["n"] if total_row else 0,
            "page": page, "page_size": page_size}


# ── Bulk (declared before /{grant_id}/... routes) ───────────────────────────

@router.post("/bulk")
async def bulk_action(body: BulkBody, current_user: dict = Depends(auth.require_role("admin"))):
    if body.action == "extend" and body.expiry is None:
        raise HTTPException(status_code=422, detail="expiry wajib untuk aksi extend.")
    if body.action == "reason_visibility" and body.reason_visible is None:
        raise HTTPException(status_code=422, detail="reason_visible wajib untuk aksi reason_visibility.")
    ids = sorted(set(body.ids))
    spec = body.expiry.model_dump() if body.expiry else None
    results: list = []
    audits: list = []
    try:
        with pg_service.get_conn() as conn:
            for gid in ids:
                try:
                    with conn.transaction():  # per-item savepoint
                        g = _lock_grant(conn, gid)
                        if body.action == "revoke":
                            entry = _do_revoke(conn, g, current_user, None)
                        elif body.action == "extend":
                            entry = _do_expiry(conn, g, current_user, spec)
                        else:
                            entry = _do_visibility(conn, g, current_user, body.reason_visible)
                    results.append({"id": gid, "ok": True, "username": g.get("grantee_name")})
                    audits.append((gid, entry))
                except GrantError as e:
                    results.append({"id": gid, "ok": False, "code": e.code})
    except Exception:
        logger.exception("bulk access grant action failed")
        raise HTTPException(status_code=500, detail="Internal server error")
    _flush_audit(current_user, audits)
    ok_count = sum(1 for r in results if r["ok"])
    return {"results": results, "ok_count": ok_count, "fail_count": len(results) - ok_count}


# ── Single mutations ────────────────────────────────────────────────────────

@router.post("/{grant_id}/revoke")
async def revoke(grant_id: str = GrantId, body: Optional[RevokeBody] = None,
                 current_user: dict = Depends(auth.require_role("admin"))):
    note = (body.note.strip() if body and body.note else None)
    _run_single(grant_id, current_user, lambda conn, g: _do_revoke(conn, g, current_user, note))
    return {"id": grant_id, "status": "revoked"}


@router.patch("/{grant_id}/expiry")
async def set_expiry(body: ExpirySpec, grant_id: str = GrantId,
                     current_user: dict = Depends(auth.require_role("admin"))):
    spec = body.model_dump()
    _run_single(grant_id, current_user, lambda conn, g: _do_expiry(conn, g, current_user, spec))
    row = pg_service.query_one(
        f"SELECT {gp.iso_z('expires_at')} AS expires_at FROM access_grants WHERE id = %s", (grant_id,))
    return {"id": grant_id, "expires_at": row["expires_at"] if row else None}


@router.patch("/{grant_id}/permission")
async def set_permission(body: PermissionBody, grant_id: str = GrantId,
                         current_user: dict = Depends(auth.require_role("admin"))):
    def _apply(conn, g):
        _require_open(g)
        if g["permission"] == body.permission:
            return None
        conn.execute("UPDATE access_grants SET permission = %s WHERE id = %s", (body.permission, g["id"]))
        gp.record_event(conn, g, "permission_changed", current_user,
                        old=g["permission"], new=body.permission)
        return ("UPDATE_ACCESS_PERMISSION",
                _audit_text(g, f"permission: {g['permission']} -> {body.permission}"))
    _run_single(grant_id, current_user, _apply)
    return {"id": grant_id, "permission": body.permission}


@router.patch("/{grant_id}/reason-visibility")
async def set_reason_visibility(body: VisibilityBody, grant_id: str = GrantId,
                                current_user: dict = Depends(auth.require_role("admin"))):
    _run_single(grant_id, current_user,
                lambda conn, g: _do_visibility(conn, g, current_user, body.reason_visible))
    return {"id": grant_id, "reason_visible": body.reason_visible}


@router.post("/{grant_id}/reveal-reason")
async def reveal_reason(grant_id: str = GrantId,
                        current_user: dict = Depends(auth.require_role("admin"))):
    def _apply(conn, g):
        gp.record_event(conn, g, "reason_revealed", current_user)
        return ("REVEAL_GRANT_REASON", f"grant={g['id']}")
    g, _ = _run_single(grant_id, current_user, _apply)
    return {"reason": g.get("reason")}
