-- 022: Track review_status for compliance_history documents
-- Values: 'draft', 'in_progress', 'needs_revision', 'done'
-- Additive and idempotent.

ALTER TABLE compliance_history ADD COLUMN IF NOT EXISTS review_status VARCHAR(32) NOT NULL DEFAULT 'draft';
CREATE INDEX IF NOT EXISTS idx_compliance_history_review_status ON compliance_history (review_status);

