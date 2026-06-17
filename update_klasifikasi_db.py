import sqlite3
import os

db_path = os.path.join("data", "legal_metadata.db")
conn = sqlite3.connect(db_path)
c = conn.cursor()

# Get all document IDs
c.execute("SELECT id FROM regulations")
docs = [row[0] for row in c.fetchall()]

if docs:
    # Set all to 'Umum' first
    c.execute("UPDATE regulations SET klasifikasi = 'Umum'")
    
    # Set some to 'Rahasia' and 'Terbatas'
    if len(docs) >= 1:
        rahasia_docs = docs[0:2] # First two
        for doc_id in rahasia_docs:
            c.execute("UPDATE regulations SET klasifikasi = 'Rahasia' WHERE id = ?", (doc_id,))
            
    if len(docs) >= 3:
        terbatas_docs = docs[2:4] # Next two
        for doc_id in terbatas_docs:
            c.execute("UPDATE regulations SET klasifikasi = 'Terbatas' WHERE id = ?", (doc_id,))
            
    conn.commit()
    print(f"Updated {len(docs)} documents. 2 Rahasia, 2 Terbatas, rest Umum.")
else:
    print("No documents found in the database.")

conn.close()
