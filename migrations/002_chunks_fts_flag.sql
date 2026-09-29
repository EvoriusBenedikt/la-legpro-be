-- ============================================================================
-- 002_chunks_fts_flag.sql — Migration M2
--
-- Tags chunks that were present in the legacy SQLite FTS5 table (chunks_fts)
-- so the M3 sparse-retrieval rewrite can reproduce today's behavior exactly:
-- the live BM25 side only searches the ~3.6k repository-ingested chunks, not
-- the full ~56k Chroma corpus. M3 can filter `WHERE sparse_legacy` for parity
-- (and widen later as a deliberate product decision).
--
-- Set to TRUE by migrations/migrate_data.py for every row loaded from
-- chunks_fts. Idempotent; auto-applied on fresh volumes via initdb.d, and
-- applied inline by migrate_data.py on already-initialized volumes.
-- ============================================================================

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS sparse_legacy BOOLEAN NOT NULL DEFAULT FALSE;
