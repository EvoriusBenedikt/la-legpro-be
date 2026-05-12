"""
Re-ingest only the 15 scanned PDFs that failed on the first pass.
Run: python vector_db/reingest_missing.py
"""
import os, sys, sqlite3, hashlib, chromadb

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "parser"))

from pdf_parser import LegalDocumentParser, LegalChunker

DB_PATH   = os.path.join(BASE_DIR, "data", "ojk_metadata.db")
CHROMA_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

MISSING_IDS = {
    "467", "465", "306", "304", "242",
    "584", "536", "11",  "329",
    "185", "184", "174", "154", "153", "147",
}

def main():
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    col = client.get_collection("ojk_regulations")

    parser = LegalDocumentParser()   # now has enable_mkldnn=False
    chunker = LegalChunker()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, judul, nomor, jenis, sektor, status, local_path
                 FROM regulations
                 WHERE local_path IS NOT NULL AND local_path != ''
                 GROUP BY local_path""")
    rows = {str(r[0]): r for r in c.fetchall()}
    conn.close()

    ok, fail = 0, 0
    for reg_id_str in MISSING_IDS:
        if reg_id_str not in rows:
            print(f"[SKIP] reg_id={reg_id_str} not in DB")
            continue

        reg_id, judul, nomor, jenis, sektor, status, local_path = rows[reg_id_str]
        filename = os.path.basename(local_path)
        print(f"\nProcessing reg_id={reg_id_str} | {jenis} {sektor} Nomor {nomor} | {filename}")

        if not os.path.exists(local_path):
            print("  [SKIP] File missing on disk")
            fail += 1
            continue

        try:
            text = parser.parse_pdf(local_path)
            if not text.strip():
                print("  [WARN] No text extracted — skipping")
                fail += 1
                continue

            meta = {
                "reg_id": reg_id_str, "filename": filename,
                "judul": str(judul) or "", "nomor": str(nomor) or "",
                "jenis": str(jenis) or "", "sektor": str(sektor) or "",
                "status": str(status) or "",
            }
            chunks = chunker.chunk_document(text, meta)
            if not chunks:
                print("  [WARN] No chunks produced")
                fail += 1
                continue

            docs, metas, ids = [], [], []
            for i, ch in enumerate(chunks):
                clean = {k: v for k, v in ch["metadata"].items() if v is not None}
                ids.append(hashlib.md5(f"{reg_id_str}_chunk_{i}".encode()).hexdigest())
                docs.append(ch["text"])
                metas.append(clean)

            col.upsert(documents=docs, metadatas=metas, ids=ids)
            print(f"  [OK] {len(docs)} chunks indexed")
            ok += 1

        except Exception as e:
            print(f"  [ERROR] {e}")
            fail += 1

    print(f"\n{'='*40}")
    print(f"Done. Success: {ok}  Failed: {fail}")
    print(f"Total chunks now: {col.count()}")

if __name__ == "__main__":
    main()
