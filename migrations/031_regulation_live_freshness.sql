-- 031: Live regulation freshness tracking, status audit history & amendment relations
-- Additive and idempotent: safe to run more than once; never drops data.

ALTER TABLE regulations
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_verification_result TEXT NOT NULL DEFAULT 'unverified',
    ADD COLUMN IF NOT EXISTS last_matched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS match_count_30d INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS status_note TEXT;

-- Normalize legal_status values across existing rows ('berlaku' | 'diubah' | 'dicabut_sebagian' | 'dicabut')
UPDATE regulations
SET legal_status = LOWER(TRIM(legal_status))
WHERE legal_status IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_regulations_heartbeat_hot
    ON regulations (last_verified_at ASC NULLS FIRST)
    WHERE legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian');

CREATE INDEX IF NOT EXISTS idx_regulations_last_matched
    ON regulations (last_matched_at DESC NULLS LAST);

-- Keep chunks.is_active_law synchronized with active statuses ('berlaku', 'diubah', 'dicabut_sebagian')
-- Note: chunks.doc_id is TEXT while regulations.id is INTEGER.
UPDATE chunks c
SET is_active_law = (r.legal_status IN ('berlaku', 'diubah', 'dicabut_sebagian'))
FROM regulations r
WHERE r.id::text = c.doc_id;

-- Audit trail of every detected status transition (for legal review & SystemMonitoring telemetry)
CREATE TABLE IF NOT EXISTS regulation_status_history (
    id TEXT PRIMARY KEY,
    regulation_id INTEGER NOT NULL REFERENCES regulations(id) ON DELETE CASCADE,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    trigger_source TEXT NOT NULL, -- 'delta_poll_cascade' | 'heartbeat_worker' | 'live_verify_ui' | 'auto_24h_check'
    related_canonical_key TEXT,
    note TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reg_status_history_reg_id
    ON regulation_status_history (regulation_id, detected_at DESC);

-- Deterministic regulation-to-regulation amendment/repeal links (independent of LLM kg_edges)
CREATE TABLE IF NOT EXISTS regulation_relations (
    id TEXT PRIMARY KEY,
    source_regulation_id INTEGER REFERENCES regulations(id) ON DELETE SET NULL,
    source_canonical_key TEXT NOT NULL,
    target_regulation_id INTEGER REFERENCES regulations(id) ON DELETE CASCADE,
    target_canonical_key TEXT NOT NULL,
    relation_type TEXT NOT NULL, -- 'AMENDED_BY' | 'PARTIALLY_REPEALED_BY' | 'REPEALED_BY'
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_canonical_key, target_canonical_key, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_reg_relations_target
    ON regulation_relations (target_regulation_id, relation_type);
