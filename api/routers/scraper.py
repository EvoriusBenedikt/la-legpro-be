import os
import sys

from fastapi import APIRouter, Depends, HTTPException
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
from services import pg_service
from services.db_service import log_audit

router = APIRouter()


SCRAPER_ROLES = {"sekretaris perusahaan", "dewa", "developer"}


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


@router.get("/scraper/config")
def get_scraper_config(current_user: dict = Depends(scraper_role)):
    try:
        return _current()
    except pg_errors.UndefinedTable:
        raise HTTPException(
            status_code=503,
            detail="Konfigurasi scraper belum tersedia (migrasi 013 belum diterapkan)",
        )


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
        raise HTTPException(
            status_code=503,
            detail="Konfigurasi scraper belum tersedia (migrasi 013 belum diterapkan)",
        )

    for scraper_id, active in changed:
        log_audit(current_user["id"], "UPDATE_SCRAPER_CONFIG", scraper_id,
                  f"active={str(active).lower()}")

    try:
        return _current()
    except pg_errors.UndefinedTable:
        raise HTTPException(
            status_code=503,
            detail="Konfigurasi scraper belum tersedia (migrasi 013 belum diterapkan)",
        )
