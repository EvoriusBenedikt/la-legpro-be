-- ============================================================================
-- 003_chunks_tsv_cap.sql — migration M2 (schema fix discovered during dry run)
--
-- The generated content_tsv column (001_schema.sql) fails with
-- "string is too long for tsvector" on chunks whose text exceeds the 1 MB
-- tsvector limit: the live Chroma corpus contains mega-document chunks
-- (one measured at ~2.7 MB of tsvector payload). FTS5/Chroma had no such
-- limit; PostgreSQL does.
--
-- Fix: cap the indexed text at the first 30,000 characters. The full text is
-- preserved in chunks.text (citations/CAG injection are unaffected); the cap
-- only limits what BM25/sparse matching sees for pathological mega-chunks
-- (normal chunks are <= ~2,000 chars). Worst-case tsvector for 30k chars is
-- ~0.5 MB, safely under the 1 MB limit.
--
-- Idempotent: acts only when the generation expression lacks the cap.
-- migration_common.ensure_schema_extras() applies the same fix to
-- already-initialized volumes (initdb.d only runs on first init).
-- Keep the cap in sync with migration_common.TSV_TEXT_CAP.
-- ============================================================================

DO $$
DECLARE
    expr text;
BEGIN
    SELECT generation_expression INTO expr
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'chunks'
      AND column_name = 'content_tsv';

    IF expr IS NULL THEN
        RAISE EXCEPTION 'chunks.content_tsv not found — apply 001_schema.sql first';
    END IF;

    IF expr NOT LIKE '%30000%' THEN
        ALTER TABLE chunks DROP COLUMN content_tsv;
        ALTER TABLE chunks ADD COLUMN content_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('simple', left(coalesce(text, ''), 30000))) STORED;
        CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (content_tsv);
    END IF;
END
$$;
