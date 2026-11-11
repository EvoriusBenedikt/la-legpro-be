-- 019: Document Editor drafts, inline comments, and version history snapshots.
-- Supports the in-place Document Editor tool in PenggunaWorkspace.
-- Additive and idempotent.

CREATE TABLE IF NOT EXISTS document_editor_drafts (
    id           TEXT PRIMARY KEY,
    report_id    TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    content_html TEXT NOT NULL,
    content_json TEXT,
    version      INT NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_editor_drafts_report ON document_editor_drafts (report_id);

CREATE TABLE IF NOT EXISTS document_editor_comments (
    id            TEXT PRIMARY KEY,
    report_id     TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL,
    author_name   TEXT NOT NULL,
    selected_text TEXT NOT NULL,
    comment_text  TEXT NOT NULL,
    replies_json  TEXT NOT NULL DEFAULT '[]',
    resolved      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_editor_comments_report ON document_editor_comments (report_id, created_at);

CREATE TABLE IF NOT EXISTS document_editor_versions (
    id             TEXT PRIMARY KEY,
    report_id      TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    version_number INT NOT NULL,
    title          TEXT NOT NULL,
    snapshot_html  TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_editor_versions_report ON document_editor_versions (report_id, version_number DESC);

