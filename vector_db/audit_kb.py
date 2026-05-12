import os, sqlite3, chromadb

BASE = r"C:\Users\ben\Documents\Programming\Lintasarta\self-dev\LegalAnalyzer"

# 1. PDFs on disk
pdf_dir = os.path.join(BASE, "data", "pdfs")
pdfs = [f for f in os.listdir(pdf_dir) if f.endswith(".pdf") and not f.startswith("temp_")]
print(f"=== PDFs on disk: {len(pdfs)} ===")

# 2. SQLite metadata DB
db_path = os.path.join(BASE, "data", "ojk_metadata.db")
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM regulations")
total_regs = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM regulations WHERE local_path IS NOT NULL AND local_path != ''")
downloaded = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM regulations WHERE local_path IS NULL OR local_path = ''")
not_downloaded = c.fetchone()[0]
print(f"=== SQLite DB: {total_regs} total | {downloaded} downloaded | {not_downloaded} not downloaded ===")

# 3. ChromaDB
client = chromadb.PersistentClient(path=os.path.join(BASE, "data", "chroma_db"))
col = client.get_or_create_collection("ojk_regulations")
total_chunks = col.count()
all_meta = col.get(include=["metadatas"])
reg_ids = set()
for m in all_meta["metadatas"]:
    rid = m.get("reg_id", m.get("nomor", "unknown"))
    reg_ids.add(str(rid))
print(f"=== ChromaDB: {total_chunks} chunks | {len(reg_ids)} unique regulations indexed ===")

# 4. Find GAPS
c.execute("SELECT id, nomor, local_path FROM regulations WHERE local_path IS NOT NULL AND local_path != ''")
rows = c.fetchall()
indexed_ids = set()
for m in all_meta["metadatas"]:
    rid = m.get("reg_id")
    if rid:
        indexed_ids.add(str(rid))

missing = []
for row in rows:
    if str(row[0]) not in indexed_ids:
        missing.append(f"  ID={row[0]} | {row[1]} | {os.path.basename(row[2])}")

print(f"\n=== MISSING from ChromaDB (downloaded but not indexed): {len(missing)} ===")
for m in missing[:15]:
    print(m)
if len(missing) > 15:
    print(f"  ... and {len(missing)-15} more")

conn.close()
