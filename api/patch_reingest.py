import re

FILE_PATH = r"c:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\api\reingest_all.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

# Remove the clear_chromadb() call from main()
content = content.replace("    clear_chromadb()\n", "    # clear_chromadb() # Disabled for resume mode\n")

# Add a check to find already processed IDs
processed_logic = """    from routers.repository import ingest_document_background
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
"""

# Replace the relevant part in main()
old_part = """    from routers.repository import ingest_document_background
    
    print(f"Connecting to database at {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, local_path, judul, nomor, jenis, sektor, status, klasifikasi FROM regulations WHERE local_path IS NOT NULL")
    rows = c.fetchall()
    conn.close()
    
    print(f"Found {len(rows)} documents to re-ingest.")"""

content = content.replace(old_part, processed_logic)

# Skip logic and gc.collect()
loop_logic = """        if doc_id in processed_ids:
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
            gc.collect()"""

old_loop_logic = """        try:
            # We bypass background task here and run it synchronously
            ingest_document_background(local_path, doc_id, filename, nomor, jenis, sektor, status, klasifikasi)
            success += 1
            print("  [OK] Successfully ingested.")
        except Exception as e:
            print(f"  [ERROR] Failed to ingest: {e}")
            failed += 1"""

content = content.replace(old_loop_logic, loop_logic)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(content)

print("Patch applied to reingest_all.py!")
