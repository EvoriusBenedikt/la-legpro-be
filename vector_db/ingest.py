"""Bulk regulation ingestion (Migration M4: was SQLite -> ChromaDB).

Reads every distinct local file registered in public.regulations, parses it
(hybrid PyMuPDF+VLM extraction for PDFs, direct read for .txt), chunks it with the shared
LegalChunker, embeds it with the vendored all-MiniLM-L6-v2 weights
(services.embed_service) and upserts the rows into the unified PG chunks
table.

Legacy parity notes:
  * chunk ids stay md5(f"{reg_id}_chunk_{i}") -- the M2 migration preserved
    them, so re-ingesting a document overwrites its old rows instead of
    duplicating (ON CONFLICT DO UPDATE == collection.upsert semantics).
  * sparse_legacy stays FALSE: the bulk corpus lived only in Chroma (never in
    the SQLite FTS5 index), so -- exactly like the migrated rows -- it takes
    part in dense retrieval only, not the `WHERE sparse_legacy` BM25 side.
  * the other chunk columns mirror the legacy Chroma metadata 1:1
    (doc_category/user_id stay NULL for this tool, as before). visibility
    defaults to 'public' since the 2026-09-29 corpus fix: NULL left chunks
    invisible to retrieval for EVERY user (the filter is visibility='public'
    OR user_id=<caller>; NULL matches neither), and the ON CONFLICT clause
    below rewrites visibility on every re-ingest, so without the default a
    re-ingest would reset backfilled rows to NULL. Access control is
    unchanged -- retrieve_contexts still gates every candidate through
    regulations.klasifikasi + access_grants (is_allowed). See
    la-legpro-doc/bug_reports.md.

Run in the container (supported default; needs parser + embedder deps):
    docker exec legpro-backend python /app/vector_db/ingest.py [search QUERY | force]
"""
import hashlib
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import script_env

script_env.bootstrap(needs_parser=True)

from pdf_parser import LegalChunker
from services.rag_service import extract_text_hybrid
from services import pg_service
from services.embed_service import embed_documents, embed_query

# Full-row upsert: ON CONFLICT mirrors the legacy collection.upsert (which
# replaced documents AND metadatas wholesale), so a re-ingest refreshes every
# column of the chunk.
UPSERT_CHUNK = (
    "INSERT INTO chunks (id, doc_id, text, window_context, domain, jenis, "
    "judul, nomor, sektor, status, filename, doc_category, visibility, "
    "user_id, embedding, sparse_legacy) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s::vector, FALSE) "
    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
    "window_context = EXCLUDED.window_context, domain = EXCLUDED.domain, "
    "jenis = EXCLUDED.jenis, judul = EXCLUDED.judul, nomor = EXCLUDED.nomor, "
    "sektor = EXCLUDED.sektor, status = EXCLUDED.status, "
    "filename = EXCLUDED.filename, doc_category = EXCLUDED.doc_category, "
    "visibility = EXCLUDED.visibility, user_id = EXCLUDED.user_id, "
    "embedding = EXCLUDED.embedding"
)


def get_indexed_reg_ids() -> set:
    """Return the set of doc_ids already stored in the chunks table."""
    try:
        rows = pg_service.query(
            "SELECT DISTINCT doc_id FROM chunks WHERE doc_id IS NOT NULL")
        return {r["doc_id"] for r in rows}
    except Exception:
        return set()


def ingest_documents(force_reindex=False):
    # DISTINCT ON (local_path) is the PG idiom for the legacy SQLite
    # `GROUP BY local_path` de-duplication (deterministic: lowest id wins).
    records = pg_service.query(
        "SELECT DISTINCT ON (local_path) id, domain, judul, nomor, jenis, "
        "sektor, status, local_path FROM regulations "
        "WHERE local_path IS NOT NULL AND local_path != '' "
        "ORDER BY local_path, id"
    )
    print(f"Found {len(records)} unique regulation files in DB.")

    chunker = LegalChunker()

    # Get already-indexed reg_ids so we can skip them
    already_indexed = set() if force_reindex else get_indexed_reg_ids()
    print(f"Already indexed: {len(already_indexed)} doc_ids  |  Skip mode: {not force_reindex}")

    processed = 0
    skipped_exists = 0
    skipped_missing = 0
    failed = 0
    # resolve_data_path maps legacy Windows host paths (migrated rows) onto
    # this environment's mount point before the existence check.
    to_process = [r for r in records
                  if str(r["id"]) not in already_indexed
                  and r["local_path"]
                  and os.path.exists(script_env.resolve_data_path(r["local_path"]))]
    total_new = len(to_process)
    print(f"New files to index: {total_new}  |  Will skip: {len(records) - total_new}\n")

    t_start = time.time()

    for row in records:
        reg_id_str = str(row["id"])
        domain, judul, nomor = row["domain"], row["judul"], row["nomor"]
        jenis, sektor, status = row["jenis"], row["sektor"], row["status"]
        local_path = script_env.resolve_data_path(row["local_path"])
        filename = os.path.basename(local_path.replace("\\", "/")) if local_path else ""

        # Skip already indexed
        if reg_id_str in already_indexed:
            skipped_exists += 1
            continue

        # Skip missing file
        if not local_path or not os.path.exists(local_path):
            skipped_missing += 1
            continue

        # ── Live progress line ──────────────────────────────────────────────
        elapsed = time.time() - t_start
        done = processed + failed
        eta_str = ""
        if done > 0 and total_new > 0:
            avg = elapsed / done
            remaining = avg * (total_new - done)
            m, s = divmod(int(remaining), 60)
            eta_str = f"  ETA {m:02d}:{s:02d}"
        pct = (done / total_new * 100) if total_new else 0
        bar_len = 30
        filled = int(bar_len * done / total_new) if total_new else 0
        bar = '#' * filled + '-' * (bar_len - filled)
        sys.stdout.write(
            f"\r  [{bar}] {pct:5.1f}%  {done}/{total_new}{eta_str}  {filename[:40]:40s}"
        )
        sys.stdout.flush()

        try:
            # 1. Parse — .txt files from curated scrapers are read directly
            if local_path.endswith(".txt"):
                with open(local_path, "r", encoding="utf-8") as f:
                    full_text = f.read()
            else:
                # (R1 bugfix: hybrid extraction -- PyMuPDF for digital pages,
                # VLM for scanned/garbled ones. The legacy PaddleOCR chain was
                # dead in every shipped image; see bug_reports.md.)
                full_text = extract_text_hybrid(local_path)

            if not full_text.strip():
                print(f"  [WARN] No text extracted from {filename}. Skipping.")
                failed += 1
                continue

            # 2. Chunk
            base_metadata = {
                "reg_id":   reg_id_str,
                "domain":   str(domain)  if domain  else "OJK",
                "filename": filename,
                "judul":    str(judul)   if judul   else "",
                "nomor":    str(nomor)   if nomor   else "",
                "jenis":    str(jenis)   if jenis   else "",
                "sektor":   str(sektor)  if sektor  else "",
                "status":   str(status)  if status  else "",
            }
            chunks = chunker.chunk_document(full_text, base_metadata)

            if not chunks:
                print(f"  [WARN] No chunks produced for {filename}.")
                failed += 1
                continue

            # 3. Build the payload — use reg_id (not nomor) to avoid collisions
            documents, metadatas, ids = [], [], []
            for i, chunk_data in enumerate(chunks):
                clean_meta = {k: v for k, v in chunk_data["metadata"].items() if v is not None}
                # Deterministic, collision-free ID: reg_id + chunk index
                chunk_id = f"{reg_id_str}_chunk_{i}"
                ids.append(hashlib.md5(chunk_id.encode()).hexdigest())
                documents.append(chunk_data["text"])
                metadatas.append(clean_meta)

            # 4. Embed + upsert into the unified PG chunks table
            embeddings = embed_documents(documents)
            with pg_service.get_conn() as conn:
                for chunk_id, text, meta, emb in zip(ids, documents, metadatas, embeddings):
                    conn.execute(
                        UPSERT_CHUNK,
                        (chunk_id, meta.get("reg_id"), text, meta.get("window_context"),
                         meta.get("domain"), meta.get("jenis"), meta.get("judul"),
                         meta.get("nomor"), meta.get("sektor"), meta.get("status"),
                         meta.get("filename"), meta.get("doc_category"),
                         meta.get("visibility") or "public", meta.get("user_id"), emb))
            processed += 1

        except Exception as e:
            sys.stdout.write(f"\n  [ERROR] {filename}: {e}\n")
            failed += 1

    elapsed_total = time.time() - t_start
    m_total, s_total = divmod(int(elapsed_total), 60)
    total_chunks = pg_service.query_one("SELECT COUNT(*) AS n FROM chunks")["n"]
    print("\n\n" + "="*50)
    print(f"INGESTION COMPLETE  (took {m_total:02d}m {s_total:02d}s)")
    print(f"  Newly indexed : {processed}")
    print(f"  Already existed: {skipped_exists}")
    print(f"  File missing  : {skipped_missing}")
    print(f"  Failed / empty: {failed}")
    print(f"  Total chunks in PG store: {total_chunks}")
    print("="*50)


def search_collection(query, n_results=3):
    """Dense top-N over the chunks table (was a Chroma collection.query)."""
    print(f"\nSearching for: '{query}'")
    q_vec = embed_query(query)
    with pg_service.get_conn() as conn:
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        rows = conn.execute(
            "SELECT text, jenis, nomor, sektor, embedding <=> %s::vector AS dist "
            "FROM chunks WHERE embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (q_vec, q_vec, n_results)).fetchall()
    for i, row in enumerate(rows):
        print(f"\n[{i+1}] Distance: {row['dist']:.4f}")
        print(f"Regulation: {row['jenis']} Nomor {row['nomor']} ({row['sektor']})")
        print(f"Preview: {row['text'][:300]}...")


if __name__ == "__main__":
    print("=== OJK RAG Pipeline Ingestion ===")
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else "aturan mengenai penagihan"
        search_collection(query)
    elif len(sys.argv) > 1 and sys.argv[1] == "force":
        print("Force re-index mode — all documents will be re-processed")
        ingest_documents(force_reindex=True)
    else:
        ingest_documents()
