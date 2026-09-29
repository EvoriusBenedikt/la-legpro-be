"""
fast_ingest_txt.py
==================
Quickly ingest all un-indexed .txt regulation files from public.regulations
into the unified PG chunks table (Migration M4: was SQLite -> ChromaDB).
Bypasses PaddleOCR completely -- runs in seconds, not minutes.

Legacy parity notes:
  * chunk ids stay the plain f"{reg_id}_c{i}" scheme the Chroma collection
    used (the M2 migration preserved them); ON CONFLICT DO NOTHING mirrors
    collection.add, which refused to touch existing ids.
  * visibility='public' is set exactly like the legacy metadata (this tool --
    unlike ingest.py -- always published what it ingested).
  * sparse_legacy stays FALSE (bulk corpus was dense-only in legacy).

Run in the container (supported default; needs the embedder):
    docker exec legpro-backend python /app/vector_db/fast_ingest_txt.py
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
sys.path.insert(0, BASE_DIR)

import script_env

script_env.bootstrap()

from services import pg_service
from services.embed_service import embed_documents

CHUNK_SIZE   = 800   # characters per chunk
CHUNK_OVERLAP = 100

INSERT_CHUNK = (
    "INSERT INTO chunks (id, doc_id, text, window_context, domain, jenis, "
    "judul, nomor, sektor, status, filename, doc_category, visibility, "
    "user_id, embedding, sparse_legacy) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s::vector, FALSE) "
    "ON CONFLICT (id) DO NOTHING"
)


def chunk_text(text: str, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Simple sliding-window chunker."""
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


def main():
    # Get already-indexed reg_ids (was a full Chroma metadata scan; one
    # indexed DISTINCT query now).
    rows = pg_service.query(
        "SELECT DISTINCT doc_id FROM chunks WHERE doc_id IS NOT NULL")
    indexed_ids = {r["doc_id"] for r in rows}
    print(f"Already indexed: {len(indexed_ids)} doc_ids")

    # ── Fetch only .txt rows from public.regulations ────────────────────────
    # (DISTINCT ON replaces the legacy SQLite `GROUP BY local_path` de-dup.)
    rows = pg_service.query(
        "SELECT DISTINCT ON (local_path) id, domain, judul, nomor, jenis, "
        "sektor, status, local_path FROM regulations "
        "WHERE local_path LIKE %s AND local_path IS NOT NULL "
        "ORDER BY local_path, id",
        ("%.txt",),
    )
    print(f"Found {len(rows)} .txt regulation files to check.\n")

    added_docs = 0
    skipped    = 0

    for row in rows:
        reg_id_str = str(row["id"])
        domain, judul, nomor = row["domain"], row["judul"], row["nomor"]
        jenis, sektor, status = row["jenis"], row["sektor"], row["status"]
        # resolve legacy Windows host paths onto this environment's mount
        local_path = script_env.resolve_data_path(row["local_path"])

        # Skip if already indexed
        if reg_id_str in indexed_ids:
            skipped += 1
            continue

        if not os.path.exists(local_path):
            print(f"  [SKIP] File not found: {local_path}")
            continue

        with open(local_path, "r", encoding="utf-8") as f:
            text = f.read()

        if not text.strip():
            print(f"  [SKIP] Empty file: {local_path}")
            continue

        chunks = chunk_text(text)
        ids, docs, metas = [], [], []

        for i, chunk in enumerate(chunks):
            chunk_id = f"{reg_id_str}_c{i}"
            ids.append(chunk_id)
            docs.append(chunk)
            metas.append({
                "reg_id":     reg_id_str,
                "domain":     domain or "Unknown",
                "judul":      (judul or "")[:200],
                "nomor":      (nomor or "")[:100],
                "jenis":      (jenis or "")[:100],
                "sektor":     (sektor or "")[:100],
                "status":     (status or "")[:50],
                "filename":   os.path.basename(local_path.replace("\\", "/")),
                "visibility": "public",
            })

        embeddings = embed_documents(docs)
        with pg_service.get_conn() as conn:
            for chunk_id, doc, meta, emb in zip(ids, docs, metas, embeddings):
                conn.execute(
                    INSERT_CHUNK,
                    (chunk_id, meta["reg_id"], doc, None, meta["domain"],
                     meta["jenis"], meta["judul"], meta["nomor"], meta["sektor"],
                     meta["status"], meta["filename"], None, meta["visibility"],
                     None, emb))
        added_docs += len(chunks)
        print(f"  [OK] {nomor:40s} -> {len(chunks)} chunks added")

    print(f"\n{'='*50}")
    print(f"  Done!  Added {added_docs} chunks from {len(rows)-skipped} new files.")
    print(f"  Skipped {skipped} already-indexed files.")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
