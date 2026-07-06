import sqlite3
import bcrypt

conn = sqlite3.connect('data/users.db')
c = conn.cursor()
new_password = b"Lintasarta2026!"
hashed = bcrypt.hashpw(new_password, bcrypt.gensalt()).decode('utf-8')

c.execute("UPDATE users SET password_hash = ? WHERE email = 'demoacc@gmail.com'", (hashed,))
conn.commit()
conn.close()
print("Reset")
