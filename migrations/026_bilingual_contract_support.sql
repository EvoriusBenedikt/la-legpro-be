-- 026: Bilingual side-by-side contract metadata on compliance_history.
-- Additive and idempotent.

ALTER TABLE compliance_history
    ADD COLUMN IF NOT EXISTS is_bilingual BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS governing_language VARCHAR(8),
    ADD COLUMN IF NOT EXISTS layout_mode VARCHAR(32) NOT NULL DEFAULT 'monolingual';

CREATE INDEX IF NOT EXISTS idx_compliance_history_bilingual
    ON compliance_history (notebook_id, is_bilingual)
    WHERE is_bilingual = TRUE;
