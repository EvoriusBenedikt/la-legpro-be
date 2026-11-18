"""Daily 'New Arrivals' delta poller & cascading invalidation service."""
from __future__ import annotations

import datetime
import uuid
from typing import Iterable, Optional

from services import pg_service
from services.freshness.status_updater import cascade_relation_to_target
from services.scraper.adapters import PORTAL_BY_KEY, get_adapter
from services.scraper.canonicalizer import build_canonical_key, normalize_jenis, normalize_nomor_tahun
from services.scraper.category_classifier import classify_regulation_category

DEFAULT_DELTA_PORTALS = ("bpk", "peraturan_go_id", "kominfo", "ojk", "bi")


def run_delta_poll(
    portals: Iterable[str] = DEFAULT_DELTA_PORTALS,
    max_pages: int = 3,
    min_year: Optional[int] = None,
) -> dict:
    """Scan pages 1..max_pages of priority portals for net-new regulations and cascade repeals/amendments."""
    if min_year is None:
        min_year = max(2020, datetime.datetime.now().year - 1)

    batch_id = f"delta_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    summary = {
        "batch_id": batch_id,
        "portals_checked": 0,
        "items_seen": 0,
        "enqueued_new": 0,
        "cascaded_updates": 0,
        "errors": [],
    }

    for portal_key in portals:
        if portal_key not in PORTAL_BY_KEY:
            continue
        summary["portals_checked"] += 1
        try:
            adapter = get_adapter(portal_key)
            listings = adapter.fetch_listings(min_year=min_year, max_pages=max_pages, rate_limit_ms=1200)
        except Exception as e:
            summary["errors"].append(f"{portal_key}: {e}")
            continue

        if not listings:
            continue

        page_keys: list[str] = []
        enriched_items: list[tuple[str, dict]] = []
        for item in listings:
            jenis = normalize_jenis(item.get("jenis"))
            norm_nomor, norm_tahun = normalize_nomor_tahun(item.get("nomor"), item.get("tahun"))
            canon = build_canonical_key(jenis, norm_nomor, norm_tahun)
            page_keys.append(canon)
            enriched_items.append((canon, {**item, "jenis": jenis, "nomor": norm_nomor, "tahun": norm_tahun}))

        with pg_service.get_conn() as conn:
            existing_rows = conn.execute(
                "SELECT canonical_key FROM regulations WHERE canonical_key = ANY(%s)",
                (page_keys,),
            ).fetchall()
            known_keys = {r["canonical_key"] for r in existing_rows}

            for canon, item in enriched_items:
                summary["items_seen"] += 1
                if canon in known_keys:
                    continue

                known_keys.add(canon)
                detail_url = item.get("detail_url") or f"canonical://{canon}"
                cat_code = classify_regulation_category(
                    portal_key=portal_key,
                    jenis=item.get("jenis") or "",
                    judul=item.get("judul") or item.get("tentang") or "",
                    raw_subject=item.get("sektor") or "",
                    sektor=item.get("sektor") or "",
                )
                conn.execute(
                    "INSERT INTO scraper_crawl_queue "
                    "(id, batch_id, portal_key, source_url, canonical_key, jenis, nomor, tahun, "
                    "tentang, raw_subject, mapped_category_code, legal_status, pdf_url, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued') "
                    "ON CONFLICT (source_url) DO NOTHING",
                    (
                        uuid.uuid4().hex,
                        batch_id,
                        portal_key,
                        detail_url,
                        canon,
                        item.get("jenis"),
                        item.get("nomor"),
                        item.get("tahun"),
                        item.get("tentang") or item.get("judul"),
                        item.get("sektor") or "",
                        cat_code,
                        item.get("legal_status") or "berlaku",
                        item.get("download_url") or "",
                    ),
                )
                summary["enqueued_new"] += 1

                # Inspect detail HTML for outgoing Mencabut / Mengubah metadata
                relations = list(item.get("relations") or [])
                if not relations and detail_url.startswith("http"):
                    meta = adapter.fetch_status_metadata(detail_url, timeout=(2.0, 3.0))
                    if meta.get("ok"):
                        relations = meta.get("relations") or []

                for rel in relations:
                    if rel.get("direction", "outgoing") != "outgoing":
                        continue
                    changed = cascade_relation_to_target(
                        conn,
                        source_canonical_key=canon,
                        source_regulation_id=None,
                        target_canonical_key=rel["canonical_key"],
                        relation_type=rel["relation_type"],
                        trigger_source="delta_poll_cascade",
                        note=f"{rel['relation_type']} {canon}",
                    )
                    if changed:
                        summary["cascaded_updates"] += 1

    return summary
