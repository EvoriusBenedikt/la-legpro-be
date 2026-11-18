import os
import sys

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, StrictBool
from psycopg import errors as pg_errors

# The runner is outside api/, so make its backend-root import explicit for
# local uvicorn/test entry points as well as the container entry point.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import run_all_scrapers

SCRAPERS = run_all_scrapers.SCRAPERS
SCRAPER_DIR = os.path.join(BASE_DIR, "scraper", "jdih")
if SCRAPER_DIR in sys.path:
    sys.path.remove(SCRAPER_DIR)
REGISTRY = [(module_name, label) for label, module_name, _ in SCRAPERS]
REGISTRY_BY_ID = dict(REGISTRY)

from auth import get_current_user
from services import pg_service, scraper_runner
from services.db_service import log_audit

router = APIRouter()


SCRAPER_ROLES = {"sekretaris perusahaan", "admin", "insinyur ti", "dewa", "developer"}


def scraper_role(current_user: dict = Depends(get_current_user)):
    if current_user.get("role", "pengguna").lower() not in SCRAPER_ROLES:
        raise HTTPException(status_code=403, detail="Akses ditolak.")
    return current_user


class ScraperUpdate(BaseModel):
    id: str
    active: StrictBool


class ScraperConfigUpdate(BaseModel):
    scrapers: list[ScraperUpdate] = Field(max_length=len(REGISTRY))


def _current():
    rows = pg_service.query("SELECT scraper_id, active FROM scraper_config")
    active_by_id = {row["scraper_id"]: bool(row["active"]) for row in rows}
    return {
        "scrapers": [
            {"id": scraper_id, "name": name, "active": active_by_id.get(scraper_id, True)}
            for scraper_id, name in REGISTRY
        ]
    }


def _config_error():
    return HTTPException(
        status_code=503,
        detail="Konfigurasi scraper belum tersedia (migrasi 013 belum diterapkan)",
    )


@router.get("/scraper/config")
def get_scraper_config(current_user: dict = Depends(scraper_role)):
    try:
        return _current()
    except pg_errors.UndefinedTable:
        raise _config_error()


@router.put("/scraper/config")
def update_scraper_config(
    req: ScraperConfigUpdate,
    current_user: dict = Depends(scraper_role),
):
    seen = set()
    for scraper in req.scrapers:
        if scraper.id not in REGISTRY_BY_ID:
            raise HTTPException(status_code=400, detail=f"Scraper tidak dikenal: {scraper.id}")
        if scraper.id in seen:
            raise HTTPException(status_code=400, detail=f"Scraper duplikat: {scraper.id}")
        seen.add(scraper.id)

    changed = []
    try:
        with pg_service.get_conn() as conn:
            rows = conn.execute(
                "SELECT scraper_id, active FROM scraper_config WHERE scraper_id = ANY(%s) FOR UPDATE",
                (list(seen),),
            ).fetchall()
            previous = {row["scraper_id"]: bool(row["active"]) for row in rows}
            for scraper in req.scrapers:
                if previous.get(scraper.id, True) != scraper.active:
                    changed.append((scraper.id, scraper.active))
                conn.execute(
                    "INSERT INTO scraper_config (scraper_id, active, updated_at, updated_by) "
                    "VALUES (%s, %s, NOW(), %s) "
                    "ON CONFLICT (scraper_id) DO UPDATE SET active = EXCLUDED.active, "
                    "updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by",
                    (scraper.id, scraper.active, str(current_user["id"])),
                )
    except pg_errors.UndefinedTable:
        raise _config_error()

    for scraper_id, active in changed:
        log_audit(current_user["id"], "UPDATE_SCRAPER_CONFIG", scraper_id,
                  f"active={str(active).lower()}")

    try:
        return _current()
    except pg_errors.UndefinedTable:
        raise _config_error()


class SourcePatchRequest(BaseModel):
    enabled: bool | None = None
    min_year: int | None = Field(default=None, ge=1945, le=2035)
    max_pages_per_run: int | None = Field(default=None, ge=1, le=100)
    skip_repealed: bool | None = None
    on_duplicate: str | None = Field(default=None, pattern="^(skip|replace)$")


class BulkToggleRequest(BaseModel):
    domain_group: str | None = None
    enabled: bool


class BroadScrapeRequest(BaseModel):
    source_keys: list[str] | None = None
    domain_group: str | None = None
    dry_run: bool = False
    on_duplicate: str | None = Field(default=None, pattern="^(skip|replace)$")


class BulkSeedRequest(BaseModel):
    categories: list[str] = []
    source_mode: str = Field(default="all", pattern="^(all|national|sectoral)$")
    min_year: int = Field(default=2015, ge=1945, le=2035)
    active_only: bool = True


@router.get("/scraper/coverage")
def get_scraper_coverage(current_user: dict = Depends(scraper_role)):
    from services.scraper.bulk_ingestor import get_coverage_stats
    return get_coverage_stats()


@router.post("/scraper/bulk/start")
def start_bulk_seeder(req: BulkSeedRequest, current_user: dict = Depends(scraper_role)):
    from services.scraper.bulk_ingestor import start_bulk_seed
    log_audit(current_user["id"], "SCRAPE_STARTED", "bulk_seeder", f"categories={len(req.categories)} mode={req.source_mode}")
    return start_bulk_seed(
        categories=req.categories,
        source_mode=req.source_mode,
        min_year=req.min_year,
        active_only=req.active_only,
    )


@router.get("/scraper/bulk/status")
def get_bulk_seeder_status(batch_id: str | None = None, current_user: dict = Depends(scraper_role)):
    from services.scraper.bulk_ingestor import get_bulk_status
    return get_bulk_status(batch_id)


@router.post("/scraper/bulk/pause")
def pause_bulk_seeder(current_user: dict = Depends(scraper_role)):
    from services.scraper.bulk_ingestor import pause_queue
    return pause_queue()


@router.post("/scraper/bulk/resume")
def resume_bulk_seeder(current_user: dict = Depends(scraper_role)):
    from services.scraper.bulk_ingestor import resume_queue
    return resume_queue()


@router.post("/scraper/bulk/retry-failed")
def retry_failed_bulk_seeder(current_user: dict = Depends(scraper_role)):
    from services.scraper.bulk_ingestor import retry_failed
    return retry_failed()


@router.get("/scraper/sources")
def get_scraper_sources(current_user: dict = Depends(scraper_role)):
    from services.scraper.adapters import PORTAL_REGISTRY
    from services.scraper.pipeline import seed_scraper_configs

    try:
        seed_scraper_configs()
        rows = pg_service.query("SELECT * FROM scraper_config")
    except pg_errors.UndefinedTable:
        raise _config_error()

    by_id = {r["scraper_id"]: r for r in rows}
    sources = []
    for p in PORTAL_REGISTRY:
        r = by_id.get(p.source_key, {})
        sources.append({
            "source_key": p.source_key,
            "display_name": r.get("display_name") or p.display_name,
            "base_url": r.get("base_url") or p.base_url,
            "domain_group": r.get("domain_group") or p.domain_group,
            "adapter_type": r.get("adapter_type") or p.adapter_type,
            "enabled": bool(r.get("active", True)),
            "min_year": int(r.get("min_year") or 2015),
            "max_pages_per_run": int(r.get("max_pages_per_run") or 10),
            "rate_limit_ms": int(r.get("rate_limit_ms") or 1200),
            "skip_repealed": bool(r.get("skip_repealed", True)),
            "on_duplicate": r.get("on_duplicate") or "skip",
            "last_scraped_at": r["last_scraped_at"].isoformat() if r.get("last_scraped_at") else None,
            "total_indexed": int(r.get("total_indexed") or 0),
            "total_deduplicated": int(r.get("total_deduplicated") or 0),
            "last_error": r.get("last_error"),
        })
    return {"sources": sources}


@router.patch("/scraper/sources/{source_key}")
def patch_scraper_source(
    source_key: str,
    req: SourcePatchRequest,
    current_user: dict = Depends(scraper_role),
):
    from services.scraper.adapters import PORTAL_BY_KEY
    from services.scraper.pipeline import seed_scraper_configs

    if source_key not in PORTAL_BY_KEY:
        raise HTTPException(status_code=404, detail=f"Scraper tidak dikenal: {source_key}")

    seed_scraper_configs()
    updates: list[str] = ["updated_at = NOW()", "updated_by = %s"]
    params: list = [str(current_user["id"])]

    if req.enabled is not None:
        updates.append("active = %s")
        params.append(req.enabled)
    if req.min_year is not None:
        updates.append("min_year = %s")
        params.append(req.min_year)
    if req.max_pages_per_run is not None:
        updates.append("max_pages_per_run = %s")
        params.append(req.max_pages_per_run)
    if req.skip_repealed is not None:
        updates.append("skip_repealed = %s")
        params.append(req.skip_repealed)
    if req.on_duplicate is not None:
        updates.append("on_duplicate = %s")
        params.append(req.on_duplicate)

    params.append(source_key)
    pg_service.execute(f"UPDATE scraper_config SET {', '.join(updates)} WHERE scraper_id = %s", tuple(params))
    log_audit(current_user["id"], "UPDATE_SCRAPER_CONFIG", source_key, "patch_source")
    return get_scraper_sources(current_user)


@router.post("/scraper/sources/bulk-toggle")
def bulk_toggle_scraper_sources(
    req: BulkToggleRequest,
    current_user: dict = Depends(scraper_role),
):
    from services.scraper.pipeline import seed_scraper_configs

    seed_scraper_configs()
    if req.domain_group:
        pg_service.execute(
            "UPDATE scraper_config SET active = %s, updated_at = NOW(), updated_by = %s "
            "WHERE domain_group = %s",
            (req.enabled, str(current_user["id"]), req.domain_group),
        )
    else:
        pg_service.execute(
            "UPDATE scraper_config SET active = %s, updated_at = NOW(), updated_by = %s",
            (req.enabled, str(current_user["id"])),
        )
    log_audit(current_user["id"], "UPDATE_SCRAPER_CONFIG", req.domain_group or "all", f"enabled={req.enabled}")
    return get_scraper_sources(current_user)


@router.post("/scraper/run")
async def start_scraper_run(
    req: BroadScrapeRequest | None = Body(default=None),
    current_user: dict = Depends(scraper_role),
):
    if req is not None:
        from services.scraper.pipeline import run_portal_scrape

        log_audit(
            current_user["id"],
            "SCRAPE_STARTED",
            req.domain_group or "",
            f"dry_run={req.dry_run} on_duplicate={req.on_duplicate}",
        )
        summary = await run_in_threadpool(
            run_portal_scrape,
            req.source_keys,
            req.domain_group,
            req.dry_run,
            req.on_duplicate,
        )
        log_audit(current_user["id"], "SCRAPE_FINISHED", "", f"indexed={summary['newly_indexed']} deduped={summary['deduplicated']}")
        return summary

    def audit_started():
        log_audit(current_user["id"], "SCRAPE_STARTED", "", f"sources={len(scraper_runner.snapshot()['sources'])}")

    def audit_finished(state):
        log_audit(current_user["id"], "SCRAPE_FINISHED" if state == "completed" else "SCRAPE_FAILED")

    try:
        result = scraper_runner.start(current_user, audit_finished, audit_started)
    except scraper_runner.AlreadyRunning:
        raise HTTPException(status_code=409, detail="Scraping sedang berjalan")
    except scraper_runner.NoActiveSources:
        raise HTTPException(status_code=400, detail="Tidak ada sumber aktif")
    except pg_errors.UndefinedTable:
        raise _config_error()
    return result


@router.get("/scraper/run")
def get_scraper_run(current_user: dict = Depends(scraper_role)):
    return scraper_runner.snapshot()
