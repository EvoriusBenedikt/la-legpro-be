-- 015: Per-user reference-base exclusion set (feature B2).
-- Stores the sources a user has DESELECTED in the reference-base checklist.
-- Empty set = every reference active ("use all knowledge"); the corpus has
-- ~952 docs so persisting exclusions (not inclusions) is the only tractable
-- model. source_ref is a tagged string: 'reg:<regulations.id>' for public law,
-- 'internal:<upload_id>' for a user's uploaded internal document (the upload_id
-- is the shared prefix of that document's internal_<upload_id>_<i> chunk ids).
-- Additive and idempotent. Existing installs (run before deploying the new backend):
--   Get-Content migrations\015_rag_source_exclusions.sql | docker exec -i legpro-postgres psql -U legpro -d legpro

CREATE TABLE IF NOT EXISTS rag_source_exclusions (
    user_id    TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_rag_source_exclusions_user
    ON rag_source_exclusions (user_id);
