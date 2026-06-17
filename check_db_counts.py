import sqlite3
import os

db_path = os.path.join("data", "legal_metadata.db")
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT COUNT(*) FROM kg_nodes")
nodes = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM kg_edges")
edges = c.fetchone()[0]
print(f"Nodes: {nodes}, Edges: {edges}")
conn.close()
