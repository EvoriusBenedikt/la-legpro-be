import sqlite3

c = sqlite3.connect(r'C:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\data\legal_metadata.db').cursor()
print("ID 972:", c.execute("SELECT id, judul, status FROM regulations WHERE id = 972").fetchall())
print("Menunggu Konfirmasi:", c.execute("SELECT id, judul, status FROM regulations WHERE status = 'Menunggu Konfirmasi'").fetchall())
