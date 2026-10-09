-- 008: Curated .txt rows -> rendered .pdf (2026-10-09, residual-gap repair).
-- The scraper _inject_curated() family registered 41 rows whose local_path
-- ends in .txt (curated text written straight into data/pdfs, download_url =
-- the HTML detail page). 40 of those .txt files were deleted from disk after
-- ingest, so serve_pdf 404s on them even though the documents are fully
-- indexed. vector_db/render_curated_pdfs.py renders each curated text to
-- <name>.pdf from the in-repo corpus lists (fallback: the .txt on disk, then
-- the row's chunks). This migration flips BOTH sides of the filename-keyed
-- sibling join (api/routers/compliance.py matches chunks.filename against
-- the local_path basename) from .txt to .pdf in one step, scoped by doc_id
-- so contract-upload chunks that happen to end in .txt are never touched.
-- Only _inject_curated ever wrote .txt local_paths, so the LIKE guards hit
-- exactly the curated population.
-- Idempotent: after the first pass no row matches the .txt patterns.
-- Order of operations: render the PDFs FIRST, then apply this (serve_pdf
-- would 404 on the new names otherwise).
-- Fresh installs: applied automatically by the postgres container
-- (./migrations is mounted at /docker-entrypoint-initdb.d).
-- Existing installs: psql "$DATABASE_URL" -f migrations/008_curated_txt_to_pdf.sql

UPDATE regulations
SET local_path = substring(local_path from '^(.*)\.txt$') || '.pdf'
WHERE local_path LIKE '%.txt';

UPDATE chunks AS c
SET filename = substring(c.filename from '^(.*)\.txt$') || '.pdf'
FROM regulations AS r
WHERE c.doc_id = r.id::text
  AND c.filename LIKE '%.txt';
