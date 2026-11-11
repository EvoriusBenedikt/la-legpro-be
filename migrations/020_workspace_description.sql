-- 020: Workspace description/notes for legal matters and contract deal rooms.
-- Additive and idempotent.

ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS description TEXT DEFAULT '';

