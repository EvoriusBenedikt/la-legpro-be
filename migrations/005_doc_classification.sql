-- 005: Confidence-gated VLM auto-classification (2026-09-30).
-- Uploaded documents are scanned + classified (secrecy klasifikasi + taxonomy
-- jenis) with a self-reported confidence 0-100; at/above
-- CLASSIFY_CONFIDENCE_THRESHOLD the doc is auto-approved and ingested,
-- below it goes to 'Menunggu Konfirmasi' for human review. The AI verdict is
-- persisted on the regulations row so auto-approvals stay auditable and the
-- pending UI can show the score + reasoning.

ALTER TABLE regulations ADD COLUMN IF NOT EXISTS ai_confidence INTEGER;
ALTER TABLE regulations ADD COLUMN IF NOT EXISTS ai_jenis TEXT;
ALTER TABLE regulations ADD COLUMN IF NOT EXISTS ai_reasoning TEXT;
-- 'auto' = confidence >= threshold, ingested without human review;
-- 'manual' = confirmed through the pending queue; NULL = pre-feature row
-- (scraped corpus / legacy uploads).
ALTER TABLE regulations ADD COLUMN IF NOT EXISTS approval_mode TEXT;
