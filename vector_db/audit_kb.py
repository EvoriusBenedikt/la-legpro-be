"""Knowledge-base coverage audit (Migration M4: was SQLite vs ChromaDB).

Compares what is on disk (data/pdfs) against public.regulations /
scraper.regulations and the unified chunks table, and lists downloaded
regulations that have no chunks (the gaps reingest_missing.py can fill).

The legacy version hardcoded an absolute BASE path from the old
"C:\\...\\LegalAnalyzer" project location (broken since the move to la-legpro);
this rewrite derives everything from the script location via script_env.

Run in the container:  docker exec legpro-backend python /app/vector_db/audit_kb.py
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import script_env

script_env.bootstrap()

from services import pg_service

# 1. Files on disk
pdf_dir = os.path.join(BASE_DIR, "data", "pdfs")
pdfs = txts = 0
if os.path.isdir(pdf_dir):
    for f in os.listdir(pdf_dir):
        if f.startswith("temp_"):
            continue
        if f.endswith(".pdf"):
            pdfs += 1
        elif f.endswith(".txt"):
            txts += 1
print(f"=== Files on disk: {pdfs} PDFs | {txts} TXTs ===")

# 2. Metadata tables
pub = pg_service.query_one(
    "SELECT COUNT(*) AS total, "
    "COUNT(*) FILTER (WHERE local_path IS NOT NULL AND local_path != '') AS downloaded "
    "FROM regulations")
scr = pg_service.query_one(
    "SELECT COUNT(*) AS total, "
    "COUNT(*) FILTER (WHERE local_path IS NOT NULL AND local_path != '') AS downloaded "
    "FROM scraper.regulations")
print(f"=== public.regulations : {pub['total']} total | {pub['downloaded']} downloaded "
      f"| {pub['total'] - pub['downloaded']} not downloaded ===")
print(f"=== scraper.regulations: {scr['total']} total | {scr['downloaded']} downloaded "
      f"| {scr['total'] - scr['downloaded']} not downloaded ===")

# 3. Unified chunk store
ch = pg_service.query_one(
    "SELECT COUNT(*) AS total, COUNT(DISTINCT doc_id) AS docs, "
    "COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS dense, "
    "COUNT(*) FILTER (WHERE sparse_legacy) AS sparse "
    "FROM chunks")
print(f"=== chunks: {ch['total']} rows | {ch['docs']} distinct doc_ids "
      f"| {ch['dense']} with embedding | {ch['sparse']} sparse_legacy ===")

# 4. Gaps: downloaded public regulations with zero chunks
missing = pg_service.query(
    "SELECT r.id, r.nomor, r.local_path FROM regulations r "
    "WHERE r.local_path IS NOT NULL AND r.local_path != '' "
    "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = r.id::text) "
    "ORDER BY r.id")
print(f"\n=== MISSING from chunks (downloaded but not indexed): {len(missing)} ===")
for m in missing[:15]:
    print(f"  ID={m['id']} | {m['nomor']} | {os.path.basename(m['local_path'] or '')}")
if len(missing) > 15:
    print(f"  ... and {len(missing) - 15} more")
