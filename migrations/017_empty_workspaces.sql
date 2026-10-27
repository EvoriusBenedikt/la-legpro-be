-- 017: Empty workspaces ("new workspace" without uploading a document).
-- A grouped notebook (014) is DERIVED from documents: notebook_id equals the
-- oldest document's id, its display name the root document's company/filename.
-- That identity contract has no room for a workspace without documents, but
-- the home page's primary CTA now creates one (user, 2026-10-27: the add
-- button "should just function as adding a clean workspace").
--
-- This table mints an INDEPENDENT workspace identity for those: the id lives
-- only here (never reused as a compliance_history id), so the two namespaces
-- are disjoint and every consumer can tell them apart cheaply. The id stays
-- the workspace's identity after documents join it: uploads pass it as
-- notebook_id, internal-reference chunks carry it as report_id, and deleting
-- the workspace card cascades its documents.
--
-- Additive and idempotent; legacy (document-rooted) notebooks are untouched.
CREATE TABLE IF NOT EXISTS notebooks (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notebooks_user ON notebooks (user_id, created_at);
