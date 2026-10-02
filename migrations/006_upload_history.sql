-- 006: Upload history tracking (2026-09-30).
-- Every /api/upload writes one row here at intake ('processing'); pipeline
-- transitions update it in place: awaiting_approval | auto_approved |
-- confirmed | rejected | failed_duplicate | failed_error | deleted.
-- The row OUTLIVES the regulations row (rejected/deleted docs), so the
-- "Riwayat Unggahan" tab is a durable trail of upload activity: who uploaded
-- what, the AI verdict, how it was resolved, and by whom.
-- Visibility (API-side): sekretaris perusahaan sees all; manajer/direktur see
-- only their own uploads (see repository.get_upload_history).

CREATE TABLE IF NOT EXISTS upload_history (
    id SERIAL PRIMARY KEY,
    doc_id TEXT NOT NULL,                -- regulations.id::text at upload time
    filename TEXT NOT NULL,
    judul TEXT,
    uploaded_by TEXT,                    -- users.id
    uploaded_by_name TEXT,               -- denormalized: survives user deletion
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome TEXT NOT NULL DEFAULT 'processing',
    ai_klasifikasi TEXT,
    ai_jenis TEXT,
    ai_confidence INTEGER,
    ai_reasoning TEXT,
    final_klasifikasi TEXT,
    final_jenis TEXT,
    resolved_at TIMESTAMPTZ,             -- last pipeline transition
    resolved_by TEXT,                    -- users.id (human resolutions only)
    resolved_by_name TEXT,
    detail TEXT                          -- failure reason / duplicate distance
);

CREATE INDEX IF NOT EXISTS idx_upload_history_uploaded_at ON upload_history (uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_upload_history_doc_id ON upload_history (doc_id);
CREATE INDEX IF NOT EXISTS idx_upload_history_uploader ON upload_history (uploaded_by);
