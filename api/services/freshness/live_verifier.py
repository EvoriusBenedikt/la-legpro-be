"""On-demand live verification, amendment-chain lookup, and freshness telemetry helpers."""
from __future__ import annotations

import datetime
from typing import Iterable

from services import pg_service
from services.freshness.status_updater import apply_status_transition, record_regulation_relation
from services.scraper.adapters import get_adapter_for_url


def _iso_or_none(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.isoformat()
    return str(dt)


def verify_regulation_live(
    regulation_id: int,
    *,
    trigger_source: str = "live_verify_ui",
    cooldown_seconds: int = 60,
) -> dict | None:
    """Synchronously verify a single regulation against its DB-stored detail_url.

    Returns None if regulation_id does not exist, or a response dict with:
      verification_state: 'verified_live' | 'cached_portal_unreachable' | 'cached_cooldown' | 'cached_no_source_url'
    """
    with pg_service.get_conn() as conn:
        reg = conn.execute(
            "SELECT id, canonical_key, legal_status, status_note, last_verified_at, "
            "last_verification_result, detail_url "
            "FROM regulations WHERE id = %s",
            (regulation_id,),
        ).fetchone()

    if not reg:
        return None

    detail_url = (reg.get("detail_url") or "").strip()
    last_verified = reg.get("last_verified_at")

    if not detail_url.startswith(("http://", "https://")):
        return {
            "regulation_id": reg["id"],
            "verification_state": "cached_no_source_url",
            "legal_status": reg.get("legal_status") or "berlaku",
            "status_note": reg.get("status_note"),
            "last_verified_at": _iso_or_none(last_verified),
            "source_url": detail_url or None,
            "status_changed": False,
        }

    if cooldown_seconds > 0 and isinstance(last_verified, datetime.datetime):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        lv_utc = last_verified if last_verified.tzinfo else last_verified.replace(tzinfo=datetime.timezone.utc)
        if (now_utc - lv_utc).total_seconds() < cooldown_seconds:
            return {
                "regulation_id": reg["id"],
                "verification_state": "cached_cooldown",
                "legal_status": reg.get("legal_status") or "berlaku",
                "status_note": reg.get("status_note"),
                "last_verified_at": _iso_or_none(last_verified),
                "source_url": detail_url,
                "status_changed": False,
            }

    adapter = get_adapter_for_url(detail_url)
    meta = adapter.fetch_status_metadata(detail_url, timeout=(2.0, 3.0))

    with pg_service.get_conn() as conn:
        if not meta.get("ok"):
            conn.execute(
                "UPDATE regulations SET last_verification_result = 'portal_unreachable' WHERE id = %s",
                (regulation_id,),
            )
            return {
                "regulation_id": reg["id"],
                "verification_state": "cached_portal_unreachable",
                "legal_status": reg.get("legal_status") or "berlaku",
                "status_note": reg.get("status_note"),
                "last_verified_at": _iso_or_none(last_verified),
                "source_url": detail_url,
                "status_changed": False,
            }

        changed = apply_status_transition(
            conn,
            regulation_id=regulation_id,
            new_status=meta["legal_status"],
            trigger_source=trigger_source,
            note=meta.get("status_note"),
        )
        for rel in meta.get("relations") or []:
            if rel.get("direction") == "incoming" and reg.get("canonical_key"):
                record_regulation_relation(
                    conn,
                    source_canonical_key=rel["canonical_key"],
                    target_canonical_key=reg["canonical_key"],
                    relation_type=rel["relation_type"],
                    target_regulation_id=regulation_id,
                    note=rel.get("raw_match"),
                )
        updated = conn.execute(
            "SELECT legal_status, status_note, last_verified_at FROM regulations WHERE id = %s",
            (regulation_id,),
        ).fetchone()

    return {
        "regulation_id": reg["id"],
        "verification_state": "verified_live",
        "legal_status": (updated or {}).get("legal_status") or meta["legal_status"],
        "status_note": (updated or {}).get("status_note"),
        "last_verified_at": _iso_or_none((updated or {}).get("last_verified_at")),
        "source_url": detail_url,
        "status_changed": changed,
    }


def get_amendment_chain(conn, regulation_ids: Iterable[int]) -> dict[int, list[dict]]:
    """Fetch AMENDS / PARTIAL_REPEAL links for a list of regulation IDs."""
    ids = sorted({int(i) for i in regulation_ids if i is not None})
    if not ids:
        return {}
    rows = conn.execute(
        """
        SELECT rr.target_regulation_id,
               rr.relation_type,
               rr.source_canonical_key,
               rr.source_regulation_id,
               rr.note,
               src.jenis,
               src.nomor,
               src.tahun,
               src.tentang,
               src.detail_url
        FROM regulation_relations rr
        LEFT JOIN regulations src ON src.id = rr.source_regulation_id
        WHERE rr.target_regulation_id = ANY(%s)
          AND rr.relation_type IN ('AMENDED_BY', 'PARTIALLY_REPEALED_BY')
        ORDER BY rr.created_at DESC
        """,
        (ids,),
    ).fetchall()
    chain: dict[int, list[dict]] = {}
    for r in rows:
        tid = r["target_regulation_id"]
        chain.setdefault(tid, []).append({
            "relation_type": r["relation_type"],
            "canonical_key": r["source_canonical_key"],
            "regulation_id": r["source_regulation_id"],
            "jenis": r.get("jenis"),
            "nomor": r.get("nomor"),
            "tahun": r.get("tahun"),
            "tentang": r.get("tentang"),
            "detail_url": r.get("detail_url"),
            "note": r.get("note"),
        })
    return chain


def record_regulation_matches(conn, regulation_ids: Iterable[int]) -> list[int]:
    """Increment 30d match telemetry and return IDs needing a 24h background freshness check."""
    ids = sorted({int(i) for i in regulation_ids if i is not None})
    if not ids:
        return []
    conn.execute(
        "UPDATE regulations "
        "SET last_matched_at = NOW(), match_count_30d = match_count_30d + 1 "
        "WHERE id = ANY(%s)",
        (ids,),
    )
    stale_rows = conn.execute(
        "SELECT id FROM regulations "
        "WHERE id = ANY(%s) "
        "  AND detail_url IS NOT NULL AND detail_url LIKE 'http%%' "
        "  AND (last_verified_at IS NULL OR last_verified_at < NOW() - INTERVAL '24 hours')",
        (ids,),
    ).fetchall()
    return [int(r["id"]) for r in stale_rows]


def refresh_stale_regulations_background(regulation_ids: list[int]) -> None:
    """BackgroundTask runner for non-blocking 24h freshness verification after compliance analysis."""
    for reg_id in regulation_ids[:10]:
        try:
            verify_regulation_live(reg_id, trigger_source="auto_24h_check", cooldown_seconds=3600)
        except Exception as e:
            print(f"[Freshness:Auto24h] reg_id={reg_id} error: {e}")


def get_freshness_summary(conn) -> dict:
    """Return coverage metrics and recent status transitions for SystemMonitoring."""
    stats = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian')) AS total_active,
            COUNT(*) FILTER (
                WHERE legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian')
                  AND last_verified_at >= NOW() - INTERVAL '7 days'
            ) AS verified_under_7d,
            COUNT(*) FILTER (
                WHERE legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian')
                  AND last_verified_at < NOW() - INTERVAL '7 days'
                  AND last_verified_at >= NOW() - INTERVAL '30 days'
            ) AS verified_7_to_30d,
            COUNT(*) FILTER (
                WHERE legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian')
                  AND last_verification_result = 'portal_unreachable'
            ) AS portal_unreachable
        FROM regulations
        """
    ).fetchone() or {}

    history = conn.execute(
        """
        SELECT h.id, h.regulation_id, h.previous_status, h.new_status,
               h.trigger_source, h.related_canonical_key, h.note, h.detected_at,
               r.jenis, r.nomor, r.tahun, r.tentang
        FROM regulation_status_history h
        JOIN regulations r ON r.id = h.regulation_id
        ORDER BY h.detected_at DESC
        LIMIT 25
        """
    ).fetchall()

    for h in history:
        h["detected_at"] = _iso_or_none(h.get("detected_at"))

    return {
        "coverage": {
            "total_active": int(stats.get("total_active") or 0),
            "verified_under_7d": int(stats.get("verified_under_7d") or 0),
            "verified_7_to_30d": int(stats.get("verified_7_to_30d") or 0),
            "portal_unreachable": int(stats.get("portal_unreachable") or 0),
        },
        "recent_transitions": history,
    }
