"""Broad multi-portal scraping & deduplicated RAG/KG ingestion pipeline."""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from typing import Optional

import requests

from services import pg_service
from services.scraper.adapters import PORTAL_BY_KEY, PORTAL_REGISTRY, get_adapter
from services.scraper.canonicalizer import (
    build_canonical_key,
    merge_or_replace_regulation,
    normalize_jenis,
    normalize_legal_status,
    normalize_nomor_tahun,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PDFS_DIR = os.path.join(os.path.dirname(BASE_DIR), "data", "pdfs")


def seed_scraper_configs() -> None:
    """Idempotently ensure all 22 portals exist in scraper_config."""
    with pg_service.get_conn() as conn:
        with conn.transaction():
            for p in PORTAL_REGISTRY:
                conn.execute(
                    "INSERT INTO scraper_config "
                    "(scraper_id, active, display_name, base_url, domain_group, adapter_type) "
                    "VALUES (%s, TRUE, %s, %s, %s, %s) "
                    "ON CONFLICT (scraper_id) DO UPDATE SET "
                    "display_name = COALESCE(scraper_config.display_name, EXCLUDED.display_name), "
                    "base_url = COALESCE(scraper_config.base_url, EXCLUDED.base_url), "
                    "domain_group = COALESCE(NULLIF(scraper_config.domain_group, ''), EXCLUDED.domain_group), "
                    "adapter_type = COALESCE(NULLIF(scraper_config.adapter_type, ''), EXCLUDED.adapter_type)",
                    (p.source_key, p.display_name, p.base_url, p.domain_group, p.adapter_type),
                )


def _upsert_kg_node_and_edges(conn, reg_id: int, item: dict) -> int:
    """Ensure a regulation node exists in kg_nodes and record any statutory edges."""
    edges_added = 0
    node_id = f"reg_{reg_id}"
    label = f"{normalize_jenis(item.get('jenis'))} {item.get('nomor') or ''}".strip()
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO kg_nodes (id, label, type, doc_id) "
                "VALUES (%s, %s, 'regulation', %s) "
                "ON CONFLICT (id) DO NOTHING",
                (node_id, label[:160], str(reg_id)),
            )
            for rel in item.get("relations") or []:
                target_id = rel.get("target_id")
                rel_type = rel.get("type", "RELATED")
                if target_id:
                    conn.execute(
                        "INSERT INTO kg_edges (source_id, target_id, relation, doc_id) "
                        "VALUES (%s, %s, %s, %s)",
                        (node_id, str(target_id), rel_type, str(reg_id)),
                    )
                    edges_added += 1
    except Exception as e:
        print(f"[Scraper] KG upsert skipped for reg {reg_id}: {e}")
    return edges_added


def _ingest_pdf_for_regulation(reg_id: int, item: dict, local_path: str) -> int:
    """Parse, chunk, embed, and upsert chunks for a single regulation PDF."""
    parser_dir = os.path.join(BASE_DIR, "parser")
    if parser_dir not in sys.path:
        sys.path.insert(0, parser_dir)
    from pdf_parser import LegalChunker
    from services.rag_service import extract_text_hybrid
    from services.embed_service import embed_documents

    full_text = extract_text_hybrid(local_path)
    if not full_text or not full_text.strip():
        return 0

    chunker = LegalChunker()
    base_meta = {
        "reg_id": str(reg_id),
        "domain": item.get("source_key", "nasional").upper(),
        "filename": os.path.basename(local_path),
        "judul": item.get("judul") or "",
        "nomor": item.get("nomor") or "",
        "jenis": normalize_jenis(item.get("jenis")),
        "sektor": item.get("sektor") or "Nasional",
        "status": "Berlaku",
    }
    chunks = chunker.chunk_document(full_text, base_meta)
    if not chunks:
        return 0

    texts = [c["text"] for c in chunks]
    embeddings = embed_documents(texts)
    with pg_service.get_conn() as conn:
        with conn.transaction():
            for i, (chunk_data, emb) in enumerate(zip(chunks, embeddings)):
                meta = chunk_data["metadata"]
                cid = hashlib.md5(f"{reg_id}_chunk_{i}".encode()).hexdigest()
                conn.execute(
                    "INSERT INTO chunks (id, doc_id, text, window_context, domain, jenis, "
                    "judul, nomor, sektor, status, filename, doc_category, visibility, "
                    "user_id, embedding, sparse_legacy) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'public', NULL, %s::vector, FALSE) "
                    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                    "window_context = EXCLUDED.window_context, embedding = EXCLUDED.embedding",
                    (
                        cid,
                        str(reg_id),
                        chunk_data["text"],
                        meta.get("window_context"),
                        meta.get("domain"),
                        meta.get("jenis"),
                        meta.get("judul"),
                        meta.get("nomor"),
                        meta.get("sektor"),
                        meta.get("status"),
                        meta.get("filename"),
                        meta.get("doc_category"),
                        emb,
                    ),
                )
    return len(chunks)


def run_portal_scrape(
    source_keys: Optional[list[str]] = None,
    domain_group: Optional[str] = None,
    dry_run: bool = False,
    on_duplicate: Optional[str] = None,
) -> dict:
    """Run scraping across selected/active portals with canonical deduplication."""
    seed_scraper_configs()
    rows = pg_service.query("SELECT * FROM scraper_config")
    cfg_by_id = {r["scraper_id"]: r for r in rows}

    targets: list[str] = []
    for p in PORTAL_REGISTRY:
        if source_keys is not None and p.source_key not in source_keys:
            continue
        if domain_group and p.domain_group != domain_group:
            continue
        cfg = cfg_by_id.get(p.source_key, {})
        if source_keys is None and not cfg.get("active", True):
            continue
        targets.append(p.source_key)

    summary = {
        "dry_run": dry_run,
        "portals_run": [],
        "fetched": 0,
        "newly_indexed": 0,
        "deduplicated": 0,
        "replaced": 0,
        "skipped_repealed": 0,
        "kg_edges_added": 0,
        "duplicates": [],
    }

    os.makedirs(PDFS_DIR, exist_ok=True)

    for key in targets:
        cfg = cfg_by_id.get(key, {})
        min_year = int(cfg.get("min_year") or 2015)
        max_pages = int(cfg.get("max_pages_per_run") or 10)
        rate_ms = int(cfg.get("rate_limit_ms") or 1200)
        skip_repealed = bool(cfg.get("skip_repealed", True))
        dup_policy = on_duplicate or cfg.get("on_duplicate") or "skip"

        portal_result = {
            "source_key": key,
            "display_name": PORTAL_BY_KEY[key].display_name,
            "status": "OK",
            "fetched": 0,
            "newly_indexed": 0,
            "deduplicated": 0,
            "replaced": 0,
            "skipped_repealed": 0,
            "error": None,
        }

        try:
            adapter = get_adapter(key)
            items = adapter.fetch_listings(min_year=min_year, max_pages=max_pages, rate_limit_ms=rate_ms)
            portal_result["fetched"] = len(items)
            summary["fetched"] += len(items)

            for item in items:
                legal_status = normalize_legal_status(item.get("legal_status"))
                if skip_repealed and legal_status == "dicabut":
                    portal_result["skipped_repealed"] += 1
                    summary["skipped_repealed"] += 1
                    continue

                if dry_run:
                    ck = build_canonical_key(item.get("jenis"), item.get("nomor"), item.get("tahun"))
                    exists = pg_service.query_one(
                        "SELECT id, judul FROM regulations WHERE canonical_key = %s LIMIT 1",
                        (ck,),
                    )
                    if exists:
                        portal_result["deduplicated"] += 1
                        summary["deduplicated"] += 1
                        summary["duplicates"].append({
                            "source_key": key,
                            "canonical_key": ck,
                            "judul": item.get("judul") or exists.get("judul"),
                            "existing_id": exists["id"],
                        })
                    else:
                        portal_result["newly_indexed"] += 1
                        summary["newly_indexed"] += 1
                    continue

                with pg_service.get_conn() as conn:
                    with conn.transaction():
                        outcome, reg_id, is_dup = merge_or_replace_regulation(conn, item, on_duplicate=dup_policy)
                        summary["kg_edges_added"] += _upsert_kg_node_and_edges(conn, reg_id, item)

                if outcome == "duplicate_skipped":
                    portal_result["deduplicated"] += 1
                    summary["deduplicated"] += 1
                    ck = build_canonical_key(item.get("jenis"), item.get("nomor"), item.get("tahun"))
                    summary["duplicates"].append({
                        "source_key": key,
                        "canonical_key": ck,
                        "judul": item.get("judul"),
                        "existing_id": reg_id,
                    })
                    continue

                dl_url = item.get("download_url") or ""
                if dl_url:
                    local_name = f"{key}_{reg_id}_{uuid.uuid4().hex[:6]}.pdf"
                    local_path = os.path.join(PDFS_DIR, local_name)
                    try:
                        r = requests.get(dl_url, timeout=30, verify=False)
                        if r.ok and r.content[:4] == b"%PDF":
                            with open(local_path, "wb") as f:
                                f.write(r.content)
                            pg_service.execute("UPDATE regulations SET local_path = %s WHERE id = %s", (local_path, reg_id))
                            _ingest_pdf_for_regulation(reg_id, item, local_path)
                    except Exception as dl_err:
                        print(f"[Scraper:{key}] PDF download/ingest skipped for {dl_url}: {dl_err}")

                if outcome == "replaced":
                    portal_result["replaced"] += 1
                    summary["replaced"] += 1
                else:
                    portal_result["newly_indexed"] += 1
                    summary["newly_indexed"] += 1

            if not dry_run:
                pg_service.execute(
                    "UPDATE scraper_config SET last_scraped_at = NOW(), "
                    "total_indexed = total_indexed + %s, "
                    "total_deduplicated = total_deduplicated + %s, "
                    "last_error = NULL WHERE scraper_id = %s",
                    (portal_result["newly_indexed"] + portal_result["replaced"], portal_result["deduplicated"], key),
                )
        except Exception as e:
            portal_result["status"] = "ERROR"
            portal_result["error"] = str(e)
            if not dry_run:
                pg_service.execute(
                    "UPDATE scraper_config SET last_error = %s WHERE scraper_id = %s",
                    (str(e)[:400], key),
                )

        summary["portals_run"].append(portal_result)

    if not dry_run and (summary["newly_indexed"] > 0 or summary["replaced"] > 0):
        from services import kb_sync
        kb_sync.mark_all_reports_stale("new_regulation_indexed")

    return summary
