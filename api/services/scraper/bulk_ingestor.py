"""Resumable PostgreSQL bulk crawl queue, BPK + JDIHN category discovery, and hydration worker."""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import urllib.parse
import uuid
from typing import Optional

import requests
from lxml import html as lhtml

from services import pg_service
from services.scraper.adapters import PORTAL_BY_KEY, PORTAL_REGISTRY, get_adapter
from services.scraper.canonicalizer import (
    build_canonical_key,
    merge_or_replace_regulation,
    normalize_jenis,
    normalize_legal_status,
    normalize_nomor_tahun,
)
from services.scraper.category_classifier import (
    LEGAL_CATEGORIES,
    classify_regulation_category,
)
from services.scraper.pipeline import PDFS_DIR, _ingest_pdf_for_regulation, _upsert_kg_node_and_edges

_WORKER_LOCK = threading.Lock()
_WORKER_RUNNING = False
_WORKER_PAUSED = False
_CURRENT_BATCH_ID: Optional[str] = None


def _discover_for_categories(
    categories: list[str],
    source_mode: str,
    min_year: int,
    active_only: bool,
) -> list[dict]:
    """Discover regulation items from portals matching the selected categories and source_mode."""
    discovered: list[dict] = []
    cat_set = set(categories) if categories else {c["code"] for c in LEGAL_CATEGORIES}

    for p in PORTAL_REGISTRY:
        if source_mode == "national" and p.source_key not in ("bpk", "peraturan_go_id", "dpr", "setkab", "mahkamah_agung"):
            continue
        if source_mode == "sectoral" and p.source_key in ("bpk", "peraturan_go_id", "dpr", "setkab"):
            continue

        default_cat = classify_regulation_category(portal_key=p.source_key, sektor=p.sektor)
        if p.source_key not in ("bpk", "peraturan_go_id", "dpr", "setkab") and default_cat not in cat_set:
            continue

        try:
            adapter = get_adapter(p.source_key)
            items = adapter.fetch_listings(min_year=min_year, max_pages=2, rate_limit_ms=600)
            for item in items:
                status = normalize_legal_status(item.get("legal_status"))
                if active_only and status == "dicabut":
                    continue
                cat_code = classify_regulation_category(
                    portal_key=p.source_key,
                    jenis=item.get("jenis"),
                    judul=item.get("judul"),
                    sektor=item.get("sektor"),
                )
                if cat_code not in cat_set:
                    continue
                item["mapped_category_code"] = cat_code
                item["legal_status"] = status
                discovered.append(item)
        except Exception as e:
            print(f"[BulkDiscovery:{p.source_key}] {e}")

    return discovered


def _resolve_and_download_pdf(detail_url: str, dl_url_hint: str, prefix: str) -> str:
    """Resolve PDF link (directly or from detail page) and save to PDFS_DIR."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    dl_url = dl_url_hint or ""

    if not dl_url and detail_url.startswith("http"):
        try:
            resp = sess.get(detail_url, timeout=15, verify=False)
            if resp.ok:
                if resp.content[:4] == b"%PDF":
                    dl_url = detail_url
                else:
                    doc = lhtml.fromstring(resp.text)
                    links = doc.xpath(
                        "//a[contains(@href,'.pdf') or contains(@href,'download') or contains(@href,'unduh') or contains(@href,'file')]/@href"
                    )
                    for href in links:
                        full = urllib.parse.urljoin(detail_url, href)
                        if any(k in full.lower() for k in (".pdf", "download", "unduh")):
                            dl_url = full
                            break
        except Exception:
            pass

    if not dl_url:
        return ""

    try:
        os.makedirs(PDFS_DIR, exist_ok=True)
        r = sess.get(dl_url, timeout=25, verify=False)
        if r.ok and r.content[:4] == b"%PDF":
            fname = f"{prefix}_{uuid.uuid4().hex[:6]}.pdf"
            lpath = os.path.join(PDFS_DIR, fname)
            with open(lpath, "wb") as f:
                f.write(r.content)
            return lpath
    except Exception:
        pass
    return ""


def _worker_loop() -> None:
    global _WORKER_RUNNING
    try:
        while True:
            if _WORKER_PAUSED:
                time.sleep(1.0)
                continue

            row = pg_service.query_one(
                "SELECT * FROM scraper_crawl_queue WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            )
            if not row:
                break

            qid = row["id"]
            pg_service.execute(
                "UPDATE scraper_crawl_queue SET status = 'processing', updated_at = NOW() WHERE id = %s",
                (qid,),
            )

            try:
                legal_status = row.get("legal_status") or "berlaku"
                cat_code = row.get("mapped_category_code") or "CIVIL_CONTRACT"
                item = {
                    "source_key": row["portal_key"],
                    "jenis": row.get("jenis"),
                    "nomor": row.get("nomor"),
                    "tahun": row.get("tahun"),
                    "judul": row.get("tentang"),
                    "tentang": row.get("tentang"),
                    "detail_url": row.get("source_url"),
                    "download_url": row.get("pdf_url") or "",
                    "legal_status": legal_status,
                    "sektor": cat_code,
                    "relations": [],
                }

                with pg_service.get_conn() as conn:
                    with conn.transaction():
                        outcome, reg_id, _ = merge_or_replace_regulation(conn, item, on_duplicate="skip")
                        conn.execute(
                            "UPDATE regulations SET category_code = %s, legal_status = %s WHERE id = %s",
                            (cat_code, legal_status, reg_id),
                        )
                        _upsert_kg_node_and_edges(conn, reg_id, item)

                if legal_status == "dicabut":
                    pg_service.execute(
                        "UPDATE scraper_crawl_queue SET status = 'skipped_repealed', updated_at = NOW() WHERE id = %s",
                        (qid,),
                    )
                    continue

                if outcome == "duplicate_skipped":
                    pg_service.execute(
                        "UPDATE scraper_crawl_queue SET status = 'dedup_merged', updated_at = NOW() WHERE id = %s",
                        (qid,),
                    )
                    continue

                lpath = _resolve_and_download_pdf(
                    row.get("source_url") or "",
                    row.get("pdf_url") or "",
                    f"{row['portal_key']}_{reg_id}",
                )
                if lpath:
                    chash = hashlib.sha256(open(lpath, "rb").read()).hexdigest()[:16]
                    pg_service.execute(
                        "UPDATE regulations SET local_path = %s, content_hash = %s WHERE id = %s",
                        (lpath, chash, reg_id),
                    )
                    _ingest_pdf_for_regulation(reg_id, item, lpath)
                    pg_service.execute(
                        "UPDATE chunks SET category_code = %s, is_active_law = TRUE WHERE doc_id = %s",
                        (cat_code, str(reg_id)),
                    )

                pg_service.execute(
                    "UPDATE scraper_crawl_queue SET status = 'done', updated_at = NOW() WHERE id = %s",
                    (qid,),
                )
            except Exception as e:
                pg_service.execute(
                    "UPDATE scraper_crawl_queue SET status = 'failed', retry_count = retry_count + 1, "
                    "error_message = %s, updated_at = NOW() WHERE id = %s",
                    (str(e)[:300], qid),
                )
    finally:
        with _WORKER_LOCK:
            _WORKER_RUNNING = False


def _ensure_worker_started() -> None:
    global _WORKER_RUNNING
    with _WORKER_LOCK:
        if not _WORKER_RUNNING:
            _WORKER_RUNNING = True
            t = threading.Thread(target=_worker_loop, daemon=True)
            t.start()


def start_bulk_seed(
    categories: list[str],
    source_mode: str = "all",
    min_year: int = 2015,
    active_only: bool = True,
) -> dict:
    global _CURRENT_BATCH_ID, _WORKER_PAUSED
    _WORKER_PAUSED = False
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    _CURRENT_BATCH_ID = batch_id

    discovered = _discover_for_categories(categories, source_mode, min_year, active_only)
    queued_count = 0

    with pg_service.get_conn() as conn:
        with conn.transaction():
            for item in discovered:
                src_url = item.get("detail_url") or ""
                if not src_url:
                    continue
                jenis = normalize_jenis(item.get("jenis"))
                norm_nomor, tahun = normalize_nomor_tahun(item.get("nomor"), item.get("tahun"))
                ck = build_canonical_key(jenis, norm_nomor, tahun)
                qid = f"q_{uuid.uuid4().hex[:10]}"
                cur = conn.execute(
                    "INSERT INTO scraper_crawl_queue "
                    "(id, batch_id, portal_key, source_url, canonical_key, jenis, nomor, tahun, "
                    "tentang, raw_subject, mapped_category_code, legal_status, pdf_url, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued') "
                    "ON CONFLICT (source_url) DO UPDATE SET "
                    "batch_id = EXCLUDED.batch_id, "
                    "status = CASE WHEN scraper_crawl_queue.status = 'failed' THEN 'queued' ELSE scraper_crawl_queue.status END, "
                    "updated_at = NOW()",
                    (
                        qid,
                        batch_id,
                        item.get("source_key") or "bpk",
                        src_url,
                        ck,
                        jenis,
                        norm_nomor,
                        tahun,
                        item.get("judul") or "",
                        item.get("sektor") or "",
                        item.get("mapped_category_code") or "CIVIL_CONTRACT",
                        item.get("legal_status") or "berlaku",
                        item.get("download_url") or "",
                    ),
                )
                queued_count += cur.rowcount or 0

    _ensure_worker_started()
    return get_bulk_status(batch_id)


def pause_queue() -> dict:
    global _WORKER_PAUSED
    _WORKER_PAUSED = True
    return get_bulk_status(_CURRENT_BATCH_ID)


def resume_queue() -> dict:
    global _WORKER_PAUSED
    _WORKER_PAUSED = False
    _ensure_worker_started()
    return get_bulk_status(_CURRENT_BATCH_ID)


def retry_failed() -> dict:
    global _WORKER_PAUSED
    _WORKER_PAUSED = False
    pg_service.execute(
        "UPDATE scraper_crawl_queue SET status = 'queued', error_message = NULL, updated_at = NOW() WHERE status = 'failed'"
    )
    _ensure_worker_started()
    return get_bulk_status(_CURRENT_BATCH_ID)


def get_bulk_status(batch_id: Optional[str] = None) -> dict:
    rows = pg_service.query(
        "SELECT status, count(*) AS n FROM scraper_crawl_queue GROUP BY status"
    )
    counts = {r["status"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    return {
        "batch_id": batch_id or _CURRENT_BATCH_ID,
        "running": _WORKER_RUNNING and not _WORKER_PAUSED,
        "paused": _WORKER_PAUSED,
        "discovered": total,
        "queued": counts.get("queued", 0) + counts.get("processing", 0),
        "embedded": counts.get("done", 0),
        "deduplicated": counts.get("dedup_merged", 0),
        "skipped_repealed": counts.get("skipped_repealed", 0),
        "failed": counts.get("failed", 0),
    }


def get_coverage_stats() -> dict:
    cat_rows = pg_service.query(
        "SELECT category_code, count(*) AS total, "
        "count(*) FILTER (WHERE COALESCE(legal_status, 'berlaku') != 'dicabut') AS active "
        "FROM regulations WHERE local_path IS NOT NULL AND local_path != '' "
        "GROUP BY category_code"
    )
    by_cat = {r["category_code"]: {"total": int(r["total"]), "active": int(r["active"])} for r in cat_rows}
    chunk_row = pg_service.query_one(
        "SELECT count(*) AS total_chunks, count(*) FILTER (WHERE is_active_law = TRUE) AS active_chunks "
        "FROM chunks WHERE doc_id IS NOT NULL"
    ) or {}
    categories_out = []
    for c in LEGAL_CATEGORIES:
        st = by_cat.get(c["code"], {"total": 0, "active": 0})
        categories_out.append({
            "code": c["code"],
            "label_en": c["label_en"],
            "label_id": c["label_id"],
            "total": st["total"],
            "active": st["active"],
        })
    return {
        "categories": categories_out,
        "total_regulations": sum(c["total"] for c in categories_out),
        "active_regulations": sum(c["active"] for c in categories_out),
        "total_chunks": int(chunk_row.get("total_chunks") or 0),
        "active_chunks": int(chunk_row.get("active_chunks") or 0),
        "queue": get_bulk_status(),
    }
