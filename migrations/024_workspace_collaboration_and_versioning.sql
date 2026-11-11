-- 024: Workspace Collaborators, Version History author tracking, and Workspace Presence.
-- Supports multi-user workspace sharing, live presence/editing status, and collaborative document editing.
-- Additive and idempotent.

CREATE TABLE IF NOT EXISTS workspace_collaborators (
    id          TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invited_by  TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'editor',
    color_index INT NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (notebook_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ws_collab_notebook ON workspace_collaborators (notebook_id);
CREATE INDEX IF NOT EXISTS idx_ws_collab_user ON workspace_collaborators (user_id);

ALTER TABLE document_editor_versions ADD COLUMN IF NOT EXISTS author_name TEXT NOT NULL DEFAULT 'Reviewer';
ALTER TABLE document_editor_versions ADD COLUMN IF NOT EXISTS color_index INT NOT NULL DEFAULT 0;
ALTER TABLE document_editor_versions ADD COLUMN IF NOT EXISTS changes_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS workspace_presence (
    report_id     TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    color_index   INT NOT NULL DEFAULT 0,
    is_typing     BOOLEAN NOT NULL DEFAULT false,
    active_clause TEXT,
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ws_presence_report ON workspace_presence (report_id, last_seen DESC);

