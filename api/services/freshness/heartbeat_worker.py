"""Tiered 'Status Heartbeat' worker for lightweight HTML-only regulation verification."""
from __future__ import annotations

import time
import urllib.parse
from collections import defaultdict

from services import pg_service
from services.freshness.status_updater import apply_status_transition, record_regulation_relation
from services.scraper.adapters import get_adapter_for_url

HOT_JENIS = ("PERMEN", "POJK", "SEOJK", "PBI", "PADG", "PERBAN", "PERKPPU")


def select_heartbeat_candidates(conn, limit: int = 40) -> list[dict]:
    """Select up to `limit` active regulations prioritized by Hot Tier (3-7d) then Foundational Tier (14-30d)."""
    return conn.execute(
        """
        SELECT id, canonical_key, jenis, nomor, tahun, legal_status, detail_url, last_verified_at
        FROM regulations
        WHERE legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian')
          AND detail_url IS NOT NULL
          AND detail_url LIKE 'http%%'
          AND (
              (
                  (last_matched_at >= NOW() - INTERVAL '30 days' OR UPPER(COALESCE(jenis, '')) = ANY(%s))
                  AND (last_verified_at IS NULL OR last_verified_at < NOW() - INTERVAL '3 days')
              )
              OR (
                  last_verified_at IS NULL OR last_verified_at < NOW() - INTERVAL '14 days'
              )
          )
        ORDER BY
            CASE
                WHEN last_matched_at >= NOW() - INTERVAL '30 days'
                  OR UPPER(COALESCE(jenis, '')) = ANY(%s) THEN 0
                ELSE 1
            END ASC,
            last_verified_at ASC NULLS FIRST
        LIMIT %s
        """,
        (list(HOT_JENIS), list(HOT_JENIS), limit),
    ).fetchall()


def run_heartbeat_batch(batch_size: int = 40, domain_jitter_s: float = 1.5) -> dict:
    """Verify up to `batch_size` regulations via HTML-only GET with per-domain circuit breaking."""
    summary = {
        "checked": 0,
        "verified_ok": 0,
        "status_changed": 0,
        "unreachable": 0,
        "circuit_skipped": 0,
    }
    domain_failures: dict[str, int] = defaultdict(int)
    last_domain_hit: dict[str, float] = {}

    with pg_service.get_conn() as conn:
        candidates = select_heartbeat_candidates(conn, limit=batch_size)

    for reg in candidates:
        detail_url = reg.get("detail_url") or ""
        host = (urllib.parse.urlparse(detail_url).hostname or "").lower()
        if domain_failures[host] >= 3:
            summary["circuit_skipped"] += 1
            continue

        elapsed = time.monotonic() - last_domain_hit.get(host, 0.0)
        if host in last_domain_hit and elapsed < domain_jitter_s:
            time.sleep(domain_jitter_s - elapsed)

        adapter = get_adapter_for_url(detail_url)
        meta = adapter.fetch_status_metadata(detail_url, timeout=(2.0, 3.0))
        last_domain_hit[host] = time.monotonic()
        summary["checked"] += 1

        with pg_service.get_conn() as conn:
            if not meta.get("ok"):
                domain_failures[host] += 1
                summary["unreachable"] += 1
                # Preserve previous last_verified_at on unreachable portals
                conn.execute(
                    "UPDATE regulations SET last_verification_result = 'portal_unreachable' WHERE id = %s",
                    (reg["id"],),
                )
                continue

            domain_failures[host] = 0
            changed = apply_status_transition(
                conn,
                regulation_id=reg["id"],
                new_status=meta["legal_status"],
                trigger_source="heartbeat_worker",
                note=meta.get("status_note"),
            )
            for rel in meta.get("relations") or []:
                if rel.get("direction") == "incoming" and reg.get("canonical_key"):
                    record_regulation_relation(
                        conn,
                        source_canonical_key=rel["canonical_key"],
                        target_canonical_key=reg["canonical_key"],
                        relation_type=rel["relation_type"],
                        target_regulation_id=reg["id"],
                        note=rel.get("raw_match"),
                    )
            if changed:
                summary["status_changed"] += 1
            else:
                summary["verified_ok"] += 1

    return summary
