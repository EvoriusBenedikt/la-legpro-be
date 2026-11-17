-- 028: Persistent AI Agent patch proposals & compliance verification results.
-- Additive and idempotent: safe to run more than once; never drops data.

CREATE TABLE IF NOT EXISTS chat_agent_actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT 'default',
    report_id TEXT NOT NULL REFERENCES compliance_history(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL DEFAULT 'batch_clause_patch',
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'applied' | 'rejected' | 'failed'
    patches_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    before_score NUMERIC,
    after_score NUMERIC,
    resolved_count INT NOT NULL DEFAULT 0,
    remaining_count INT NOT NULL DEFAULT 0,
    backup_version_id TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_chat_agent_actions_session
    ON chat_agent_actions (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_chat_agent_actions_report_status
    ON chat_agent_actions (report_id, status);
