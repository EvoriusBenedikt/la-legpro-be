import os
import sys
import sqlite3
import uuid

# Add current dir to path to import ingest
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(BASE_DIR, "vector_db"))

from ingest import ingest_documents

DB_PATH = os.path.join(BASE_DIR, "data", "legal_metadata.db")
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")

def register_local_pdfs():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found. Please ensure it is created.")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Create table if it doesn't exist (just in case)
    c.execute('''
        CREATE TABLE IF NOT EXISTS regulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT,
            judul TEXT,
            nomor TEXT,
            jenis TEXT,
            sektor TEXT,
            status TEXT,
            detail_url TEXT UNIQUE,
            download_url TEXT,
            local_path TEXT
        )
    ''')

    new_files_count = 0

    # Look for subdirectories in data/pdfs
    for item in os.listdir(PDFS_DIR):
        item_path = os.path.join(PDFS_DIR, item)
        if os.path.isdir(item_path):
            domain_name = item.capitalize() # e.g. "Kemnaker", "Kemenkeu"
            print(f"Scanning folder for domain: {domain_name}...")
            
            for file in os.listdir(item_path):
                if file.endswith('.pdf'):
                    pdf_path = os.path.join(item_path, file)
                    
                    # Check if this local_path is already registered
                    c.execute("SELECT id FROM regulations WHERE local_path = ?", (pdf_path,))
                    if c.fetchone():
                        print(f"  [SKIP] Already in DB: {file}")
                        continue
                        
                    # Prepare mock metadata
                    judul = file.replace('.pdf', '')
                    doc_id = str(uuid.uuid4())[:8]
                    nomor = f"{domain_name}-{doc_id}"
                    jenis = "Peraturan"
                    sektor = domain_name
                    status = "Berlaku"
                    
                    try:
                        c.execute('''
                            INSERT INTO regulations (domain, judul, nomor, jenis, sektor, status, detail_url, download_url, local_path)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (domain_name, judul, nomor, jenis, sektor, status, f"local://{doc_id}", "", pdf_path))
                        new_files_count += 1
                        print(f"  [ADD] Registered: {file}")
                    except sqlite3.IntegrityError:
                        print(f"  [ERROR] Database integrity error for {file}")
                        
    conn.commit()
    conn.close()
    
    print(f"\nRegistered {new_files_count} new PDFs into the SQLite database.")
    
    if new_files_count > 0:
        print("Starting AI Vector Ingestion for new files...")
        ingest_documents(force_reindex=False)
    else:
        print("No new files to ingest.")

if __name__ == "__main__":
    register_local_pdfs()
    print("Done!")
