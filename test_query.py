import sqlite3

conn = sqlite3.connect('data/legal_metadata.db')
c = conn.cursor()

c.execute("SELECT id, klasifikasi, local_path FROM regulations LIMIT 10")
print("First 10 docs:", c.fetchall())

c.execute("SELECT COUNT(*) FROM regulations WHERE local_path IS NOT NULL AND local_path != '' AND klasifikasi IN ('Umum', 'Rahasia', 'Terbatas')")
print("Query count:", c.fetchone())

conn.close()
