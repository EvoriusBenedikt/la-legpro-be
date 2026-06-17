import sqlite3

conn = sqlite3.connect('data/users.db')
conn.execute("UPDATE users SET role = 'direktur' WHERE username = 'Direktur'")
conn.commit()
print('Updated:', conn.execute("SELECT username, role FROM users WHERE username = 'Direktur'").fetchall())
conn.close()
