-- 021: Track last_accessed_at for workspaces / notebooks
-- Additive and idempotent.

ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_notebooks_last_accessed ON notebooks (user_id, last_accessed_at DESC);

