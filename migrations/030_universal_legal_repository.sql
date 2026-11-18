-- 030: Universal 14-Category Legal Repository, Category-Routed Chunks & Resumable Bulk Crawl Queue
-- Additive and idempotent: safe to run more than once; never drops data.

ALTER TABLE regulations
    ADD COLUMN IF NOT EXISTS category_code TEXT NOT NULL DEFAULT 'CIVIL_CONTRACT',
    ADD COLUMN IF NOT EXISTS sub_category TEXT,
    ADD COLUMN IF NOT EXISTS subject_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_regulations_category_status
    ON regulations (category_code, legal_status, tahun DESC);

-- Propagate category_code, is_active_law, and pasal_number onto chunks for fast pre-filtered vector search
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS category_code TEXT NOT NULL DEFAULT 'CIVIL_CONTRACT',
    ADD COLUMN IF NOT EXISTS is_active_law BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS pasal_number TEXT;

CREATE INDEX IF NOT EXISTS idx_chunks_category_active
    ON chunks (category_code, is_active_law);

-- Durable, resumable bulk crawl queue for large-scale ingestion runs
CREATE TABLE IF NOT EXISTS scraper_crawl_queue (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    portal_key TEXT NOT NULL,
    source_url TEXT NOT NULL UNIQUE,
    canonical_key TEXT,
    jenis TEXT,
    nomor TEXT,
    tahun INT,
    tentang TEXT,
    raw_subject TEXT,
    mapped_category_code TEXT NOT NULL DEFAULT 'CIVIL_CONTRACT',
    legal_status TEXT NOT NULL DEFAULT 'berlaku',
    pdf_url TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    retry_count INT NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scraper_crawl_queue_status
    ON scraper_crawl_queue (status, portal_key, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_scraper_crawl_queue_batch
    ON scraper_crawl_queue (batch_id, status);
