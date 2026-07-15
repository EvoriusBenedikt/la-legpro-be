import sqlite3
import os

DB_PATH = r"c:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\data\legal_metadata.db"
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("UPDATE regulations SET local_path = replace(local_path, 'LegalAnalyzer', 'la-legpro\\la-legpro-be') WHERE local_path LIKE '%LegalAnalyzer%'")
conn.commit()

c.execute("UPDATE regulations SET local_path = replace(local_path, '/app/data/pdfs/', 'c:\\Users\\ben\\Documents\\Programming\\Lintasarta\\self-dev\\la-legpro\\la-legpro-be\\data\\pdfs\\') WHERE local_path LIKE '%/app/%'")
conn.commit()

conn.close()
print("DB paths updated!")
