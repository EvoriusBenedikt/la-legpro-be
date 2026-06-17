import sqlite3
import bcrypt

salt = bcrypt.gensalt()
hashed = bcrypt.hashpw(b'@Engineer123', salt).decode('utf-8')

conn = sqlite3.connect('data/users.db')
try:
    conn.execute("INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)", 
                 ('engineer_id', 'engineer', 'engineer@test.com', hashed, 'insinyur ti'))
    conn.commit()
    print("Engineer created successfully.")
except Exception as e:
    print("Error:", e)
conn.close()
