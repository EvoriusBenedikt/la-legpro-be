import os
import sys
import sqlite3
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(BASE_DIR, "parser"))

DB_PATH = os.path.join(BASE_DIR, "data", "legal_metadata.db")
CHROMA_DIR = os.path.join(BASE_DIR, "data", "chromadb")

def clear_chromadb():
    print(f"Clearing existing ChromaDB at {CHROMA_DIR}...")
    if os.path.exists(CHROMA_DIR):
        try:
            shutil.rmtree(CHROMA_DIR)
            print("Successfully deleted old ChromaDB data.")
        except Exception as e:
            print(f"Failed to delete ChromaDB data: {e}")
            sys.exit(1)
    else:
        print("ChromaDB folder does not exist, proceeding...")
        
    print("Clearing chunks_fts table...")
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM chunks_fts")
        conn.commit()
        conn.close()
        print("Successfully deleted old chunks_fts data.")
    except Exception as e:
        print(f"Failed to clear chunks_fts (table might not exist yet): {e}")

def main():
    # clear_chromadb() # Disabled for resume mode
    
    # We must import from repository *after* clearing ChromaDB because importing main might initialize ChromaDB client
    from routers.repository import ingest_document_background
    import gc
    
    print(f"Connecting to database at {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, local_path, judul, nomor, jenis, sektor, status, klasifikasi FROM regulations WHERE local_path IS NOT NULL")
    rows = c.fetchall()
    
    c.execute("SELECT DISTINCT doc_id FROM chunks_fts")
    processed_ids_raw = c.fetchall()
    processed_ids = {row[0] for row in processed_ids_raw}
    conn.close()
    
    print(f"Found {len(rows)} documents. {len(processed_ids)} already processed.")

    
    success = 0
    failed = 0
    for row in rows:
        doc_id, local_path, judul, nomor, jenis, sektor, status, klasifikasi = row
        filename = os.path.basename(local_path) if local_path else f"{judul}.pdf"
        print(f"\nRe-ingesting: {filename} (ID: {doc_id})")
        if not local_path or not os.path.exists(local_path):
            print(f"  [ERROR] File missing at path: {local_path}")
            failed += 1
            continue
            
        if doc_id in processed_ids:
            print(f"  [SKIP] Document already processed.")
            success += 1
            continue
            
        try:
            # We bypass background task here and run it synchronously
            ingest_document_background(local_path, doc_id, filename, nomor, jenis, sektor, status, klasifikasi)
            success += 1
            print("  [OK] Successfully ingested.")
        except Exception as e:
            print(f"  [ERROR] Failed to ingest: {e}")
            failed += 1
        finally:
            gc.collect()
            
    print(f"\nDone! Successfully reingested: {success}, Failed: {failed}")

if __name__ == "__main__":
    main()
