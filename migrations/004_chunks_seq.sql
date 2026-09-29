-- 004: positional ordering for chunks (Migration M3 -- cutover).
--
-- The legacy CAG path (rag_service.retrieve_contexts) reconstructs a whole
-- document by concatenating every chunk ChromaDB returns for a reg_id, in
-- Chroma's internal insertion order. The unified chunks table has no
-- positional column, so that order would be lost. seq restores it:
--
--   * existing rows are numbered in physical scan order when the identity
--     column is added -- for the M2-loaded corpus that equals the Chroma
--     scan order the migration inserted in;
--   * new inserts (document uploads) receive increasing seq in insert
--     order, matching the old chunk-index order within a document;
--   * M2 tooling is unaffected: migrate_data.py inserts explicit column
--     lists without seq, and TRUNCATE ... RESTART IDENTITY resets it.
--
-- Idempotent: IF NOT EXISTS makes re-application on an initialized volume
-- a no-op. On fresh volumes this runs from docker-entrypoint-initdb.d after
-- 001_schema.sql and before any data load.

ALTER TABLE public.chunks
    ADD COLUMN IF NOT EXISTS seq bigint GENERATED ALWAYS AS IDENTITY;
