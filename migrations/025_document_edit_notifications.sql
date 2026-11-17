-- 025: Collaborative document edit notifications, per-user read states, and mutes.
-- Additive and idempotent.

ALTER TABLE compliance_history
    ADD COLUMN IF NOT EXISTS last_edited_by TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS document_user_states (
    report_id      TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_viewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (report_id, user_id)
);

CREATE TABLE IF NOT EXISTS document_notifications (
    id           TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    actor_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id    TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL DEFAULT 'document_edited',
    reference_id TEXT,
    edit_count   INT NOT NULL DEFAULT 1,
    is_read      BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_notif_recipient_unread
    ON document_notifications (recipient_id, is_read, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_doc_notif_dedup
    ON document_notifications (report_id, actor_id, recipient_id, event_type, is_read, updated_at DESC);

CREATE TABLE IF NOT EXISTS notification_mutes (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK (target_type IN ('notebook', 'document')),
    target_id   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_notif_mutes_user
    ON notification_mutes (user_id, target_type);
