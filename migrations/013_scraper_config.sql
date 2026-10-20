-- 013: Persist active/inactive state for the scraper runner and configuration API.
-- Additive and idempotent: safe to run more than once; never drops data.
-- Fresh installs: applied automatically (postgres mounts ./migrations at
--   /docker-entrypoint-initdb.d and runs files in lexicographic order).
-- Existing installs (REQUIRED before deploying the new backend):
--   psql "$DATABASE_URL" -f migrations/013_scraper_config.sql
--   or: Get-Content migrations\013_scraper_config.sql | docker exec -i legpro-postgres psql -U legpro -d legpro

CREATE TABLE IF NOT EXISTS scraper_config (
    scraper_id TEXT PRIMARY KEY,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
);
