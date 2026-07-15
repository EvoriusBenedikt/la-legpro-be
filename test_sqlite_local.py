import sqlite3
import os

db_path = r"C:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\data\legal_metadata.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

allowed_klasifikasi = ["Umum"]
klas_str = ", ".join(f"'{k}'" for k in allowed_klasifikasi)
user_id = 1

query = f"""
    SELECT count(*) 
    FROM regulations 
    WHERE local_path IS NOT NULL AND local_path != ''
    AND status != 'Menunggu Konfirmasi'
    AND (
        klasifikasi IN ({klas_str}) 
        OR id IN (
            SELECT doc_id FROM access_grants 
            WHERE granted_to = ? 
            AND (expires_at IS NULL OR expires_at = '' OR expires_at >= datetime('now'))
        )
    )
"""
c.execute(query, (user_id,))
print("Records Umum:", c.fetchone()[0])
conn.close()
