-- 014: Notebook grouping for compliance_history.
-- A notebook is a container of analyzed documents (prototype parity: uploads
-- from within a notebook land in THAT notebook's document list). The root
-- document row doubles as the notebook identity -- notebook_id equals the
-- id of the first (oldest) document of the group, so existing deep links
-- (/contracts/{id}) keep working. NULL legacy rows are backfilled to be
-- their own single-document notebook.
-- Additive and idempotent: safe to run more than once; never drops data.
-- Fresh installs: applied automatically (postgres mounts ./migrations at
--   /docker-entrypoint-initdb.d and runs files in lexicographic order).
-- Existing installs (REQUIRED before deploying the new backend):
--   Get-Content migrations\014_notebook_grouping.sql | docker exec -i legpro-postgres psql -U legpro -d legpro

ALTER TABLE compliance_history ADD COLUMN IF NOT EXISTS notebook_id TEXT;

UPDATE compliance_history SET notebook_id = id WHERE notebook_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_compliance_history_notebook
    ON compliance_history (notebook_id, created_at);
