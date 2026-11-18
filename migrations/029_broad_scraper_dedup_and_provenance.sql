-- 029: Broad multi-portal scraping, canonical deduplication, legal status & duplicate policy
-- Additive and idempotent: safe to run more than once; never drops data.

ALTER TABLE regulations
    ADD COLUMN IF NOT EXISTS canonical_key TEXT,
    ADD COLUMN IF NOT EXISTS legal_status TEXT NOT NULL DEFAULT 'berlaku',
    ADD COLUMN IF NOT EXISTS source_portals JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS source_urls JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS tahun INT,
    ADD COLUMN IF NOT EXISTS tentang TEXT,
    ADD COLUMN IF NOT EXISTS promulgation_date DATE;

-- Backfill canonical_key safely (regulations.id is INTEGER -> cast to text)
UPDATE regulations
SET canonical_key = UPPER(TRIM(COALESCE(NULLIF(jenis, ''), 'UNKNOWN')))
    || '|'
    || UPPER(TRIM(COALESCE(NULLIF(nomor, ''), id::text)))
WHERE canonical_key IS NULL;

-- Ensure any remaining duplicates don't block the unique index
WITH ranked AS (
    SELECT id, canonical_key,
           ROW_NUMBER() OVER (PARTITION BY canonical_key ORDER BY id) AS rn
    FROM regulations
    WHERE canonical_key IS NOT NULL
)
UPDATE regulations r
SET canonical_key = r.canonical_key || '#' || r.id::text
FROM ranked
WHERE r.id = ranked.id AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_regulations_canonical_key
    ON regulations (canonical_key)
    WHERE canonical_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_regulations_legal_status
    ON regulations (legal_status);

-- Extend existing scraper_config (from 013_scraper_config.sql) in-place
ALTER TABLE scraper_config
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS base_url TEXT,
    ADD COLUMN IF NOT EXISTS domain_group TEXT NOT NULL DEFAULT 'national',
    ADD COLUMN IF NOT EXISTS adapter_type TEXT NOT NULL DEFAULT 'JdihnTemplateAdapter',
    ADD COLUMN IF NOT EXISTS min_year INT NOT NULL DEFAULT 2015,
    ADD COLUMN IF NOT EXISTS max_pages_per_run INT NOT NULL DEFAULT 10,
    ADD COLUMN IF NOT EXISTS rate_limit_ms INT NOT NULL DEFAULT 1200,
    ADD COLUMN IF NOT EXISTS skip_repealed BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS on_duplicate TEXT NOT NULL DEFAULT 'skip',
    ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_indexed INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS total_deduplicated INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error TEXT;
