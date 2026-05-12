import sqlite3, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
for dbfile in ["legal_metadata.db", "ojk_metadata.db"]:
    path = os.path.join(BASE_DIR, "data", dbfile)
    if not os.path.exists(path):
        print(f"{dbfile}: NOT FOUND")
        continue
    conn = sqlite3.connect(path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"{dbfile}: tables = {tables}")
    for t in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols  = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        print(f"   {t}: {count} rows, cols={cols}")
    conn.close()
