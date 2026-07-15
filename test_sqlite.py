import sqlite3
import os

db_path = r"C:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\data\legal_metadata.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM regulations")
print("COUNT:", c.fetchone()[0])
conn.close()
