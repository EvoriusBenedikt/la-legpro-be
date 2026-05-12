import os
import sys
import sqlite3
import hashlib
import chromadb
from chromadb.utils import embedding_functions

# Add parser directory to path so we can import our Parser and Chunker
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "parser"))

from pdf_parser import LegalDocumentParser, LegalChunker

# Configuration
DB_PATH = os.path.join(BASE_DIR, "data", "legal_metadata.db")
CHROMA_DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

def init_chroma():
    print(f"Initializing ChromaDB at {CHROMA_DB_DIR}...")
    client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    collection = client.get_or_create_collection(
        name="ojk_regulations",
        metadata={"hnsw:space": "cosine"}
    )
    return client, collection

def get_indexed_reg_ids(collection) -> set:
    """Return the set of reg_ids already stored in ChromaDB."""
    try:
        result = collection.get(include=["metadatas"])
        return {m.get("reg_id", "") for m in result["metadatas"] if m.get("reg_id")}
    except Exception:
        return set()

def ingest_documents(force_reindex=False):
    if not os.path.exists(DB_PATH):
        print(f"SQLite DB not found: {DB_PATH}")
        return

    client, collection = init_chroma()
    parser = None  # Lazy-init: only created when a PDF is actually encountered
    chunker = LegalChunker()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Fetch DISTINCT (reg_id, local_path) rows — skip rows without a local file
    c.execute("""
        SELECT id, domain, judul, nomor, jenis, sektor, status, local_path
        FROM regulations
        WHERE local_path IS NOT NULL AND local_path != ''
        GROUP BY local_path        -- de-duplicate identical paths
    """)
    records = c.fetchall()
    conn.close()

    print(f"Found {len(records)} unique regulation files in DB.")

    # Get already-indexed reg_ids so we can skip them
    already_indexed = set() if force_reindex else get_indexed_reg_ids(collection)
    print(f"Already indexed: {len(already_indexed)} reg_ids  |  Skip mode: {not force_reindex}")

    processed = 0
    skipped_exists = 0
    skipped_missing = 0
    failed = 0
    to_process = [r for r in records if str(r[0]) not in already_indexed and r[7] and os.path.exists(r[7])]
    total_new = len(to_process)
    print(f"New files to index: {total_new}  |  Will skip: {len(records) - total_new}\n")

    import time
    t_start = time.time()

    for row in records:
        reg_id, domain, judul, nomor, jenis, sektor, status, local_path = row
        reg_id_str = str(reg_id)
        filename = os.path.basename(local_path) if local_path else ""

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
                # Lazy-init PaddleOCR only when we actually hit a PDF
                if parser is None:
                    print("  [INFO] Initializing PaddleOCR for PDF processing...")
                    parser = LegalDocumentParser()
                full_text = parser.parse_pdf(local_path)

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

            # 3. Build ChromaDB payload — use reg_id (not nomor) to avoid collisions
            documents, metadatas, ids = [], [], []
            for i, chunk_data in enumerate(chunks):
                clean_meta = {k: v for k, v in chunk_data["metadata"].items() if v is not None}
                # Deterministic, collision-free ID: reg_id + chunk index
                chunk_id = f"{reg_id_str}_chunk_{i}"
                ids.append(hashlib.md5(chunk_id.encode()).hexdigest())
                documents.append(chunk_data["text"])
                metadatas.append(clean_meta)

            # 4. Insert into ChromaDB (upsert-style: add will error on duplicate → use upsert)
            collection.upsert(documents=documents, metadatas=metadatas, ids=ids)
            processed += 1

        except Exception as e:
            sys.stdout.write(f"\n  [ERROR] {filename}: {e}\n")
            failed += 1

    elapsed_total = time.time() - t_start
    m_total, s_total = divmod(int(elapsed_total), 60)
    print(f"\n\n" + "="*50)
    print(f"INGESTION COMPLETE  (took {m_total:02d}m {s_total:02d}s)")
    print(f"  Newly indexed : {processed}")
    print(f"  Already existed: {skipped_exists}")
    print(f"  File missing  : {skipped_missing}")
    print(f"  Failed / empty: {failed}")
    print(f"  Total chunks in ChromaDB: {collection.count()}")
    print("="*50)


def search_collection(query, n_results=3):
    client, collection = init_chroma()
    print(f"\nSearching for: '{query}'")
    results = collection.query(query_texts=[query], n_results=n_results)
    for i in range(len(results['documents'][0])):
        doc = results['documents'][0][i]
        meta = results['metadatas'][0][i]
        dist = results['distances'][0][i]
        print(f"\n[{i+1}] Distance: {dist:.4f}")
        print(f"Regulation: {meta.get('jenis')} Nomor {meta.get('nomor')} ({meta.get('sektor')})")
        print(f"Preview: {doc[:300]}...")


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
