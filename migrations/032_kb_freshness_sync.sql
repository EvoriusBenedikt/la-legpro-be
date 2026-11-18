-- 032: Track knowledge-base staleness & sync timestamp on compliance_history
-- Additive and idempotent: safe to run more than once; never drops data.

ALTER TABLE compliance_history
    ADD COLUMN IF NOT EXISTS kb_synced_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS kb_stale BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS kb_stale_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_compliance_history_kb_stale
    ON compliance_history (kb_stale)
    WHERE kb_stale = TRUE;
