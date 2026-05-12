"""
fast_ingest_txt.py
==================
Quickly ingest all un-indexed .txt regulation files from SQLite → ChromaDB.
Bypasses PaddleOCR completely — runs in seconds, not minutes.
"""

import os
import sys
import sqlite3
import chromadb

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
DB_PATH      = os.path.join(BASE_DIR, "data", "legal_metadata.db")
CHROMA_DIR   = os.path.join(BASE_DIR, "data", "chroma_db")
CHUNK_SIZE   = 800   # characters per chunk
CHUNK_OVERLAP = 100

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
    # ── Connect to ChromaDB ──────────────────────────────────────────────────
    print("Connecting to ChromaDB...")
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    collection = client.get_or_create_collection(
        name="ojk_regulations",
        metadata={"hnsw:space": "cosine"}
    )

    # Get already-indexed reg_ids (fast: uses metadata filter)
    try:
        existing = collection.get(include=["metadatas"], where={"$exists": True})
        indexed_ids = {m.get("reg_id", "") for m in existing["metadatas"]}
    except Exception:
        indexed_ids = set()
    print(f"Already indexed: {len(indexed_ids)} chunks")

    # ── Fetch only .txt rows from SQLite ────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, domain, judul, nomor, jenis, sektor, status, local_path
        FROM regulations
        WHERE local_path LIKE '%.txt'
          AND local_path IS NOT NULL
        GROUP BY local_path
    """)
    rows = c.fetchall()
    conn.close()

    print(f"Found {len(rows)} .txt regulation files to check.\n")

    added_docs = 0
    skipped    = 0

    for row in rows:
        reg_id, domain, judul, nomor, jenis, sektor, status, local_path = row
        reg_id_str = str(reg_id)

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
                "filename":   os.path.basename(local_path),
                "visibility": "public",
            })

        collection.add(ids=ids, documents=docs, metadatas=metas)
        added_docs += len(chunks)
        print(f"  [OK] {nomor:40s} -> {len(chunks)} chunks added")

    print(f"\n{'='*50}")
    print(f"  Done!  Added {added_docs} chunks from {len(rows)-skipped} new files.")
    print(f"  Skipped {skipped} already-indexed files.")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
