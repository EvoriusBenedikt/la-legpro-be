import sqlite3
conn = sqlite3.connect('data/users.db')
c = conn.cursor()
c.execute("SELECT username, email FROM users")
for row in c.fetchall():
    print(row)
conn.close()
