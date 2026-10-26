-- 016: Per-workspace scoping for private internal references (feature B3).
-- Adds a nullable report_id to chunks. For a PRIVATE internal-reference chunk
-- (visibility='private', doc_id IS NULL, id LIKE 'internal\_%'), report_id =
-- the workspace (notebook) it was uploaded into; the LEFT reference list and
-- both retrieval paths narrow a user's internal refs to the current workspace.
-- report_id is NULL for: all public-law rows (they stay global), and every
-- internal chunk ingested BEFORE B3 (unscoped legacy — kept visible to its
-- owner in every workspace, see the design's NULL-report_id rule). Additive
-- and idempotent. Existing installs (run before deploying the new backend):
--   Get-Content migrations\016_chunks_report_id.sql | docker exec -i legpro-postgres psql -U legpro -d legpro

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS report_id TEXT;

-- Backs the private-chunk workspace narrowing; partial so it stays small
-- (public-law rows are the overwhelming majority and all carry report_id NULL).
CREATE INDEX IF NOT EXISTS idx_chunks_private_report
    ON chunks (user_id, report_id)
    WHERE visibility = 'private' AND report_id IS NOT NULL;
