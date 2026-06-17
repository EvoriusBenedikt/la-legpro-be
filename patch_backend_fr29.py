import os

filepath = os.path.join("api", "main.py")
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add table creation
table_init_code = """    c.execute('''CREATE TABLE IF NOT EXISTS kg_exclusions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # FR-29: Document Taxonomy
    c.execute('''CREATE TABLE IF NOT EXISTS document_taxonomy (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        parent_id INTEGER,
        is_active BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Seed default taxonomy if empty
    c.execute("SELECT COUNT(*) FROM document_taxonomy")
    if c.fetchone()[0] == 0:
        default_types = [
            "Peraturan Pemerintah",
            "Undang-Undang",
            "Peraturan OJK",
            "Surat Edaran OJK",
            "Dokumen Internal",
            "Peraturan Menteri",
            "Regulasi Custom"
        ]
        for dt in default_types:
            c.execute("INSERT INTO document_taxonomy (name) VALUES (?)", (dt,))
"""

old_table_init = """    c.execute('''CREATE TABLE IF NOT EXISTS kg_exclusions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')"""

if "CREATE TABLE IF NOT EXISTS document_taxonomy" not in content:
    content = content.replace(old_table_init, table_init_code)

# 2. Add API Routes for taxonomy
api_routes = """
# --- FR-29: Document Taxonomy Management ---
from pydantic import BaseModel
from typing import Optional

class TaxonomyCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None

class TaxonomyUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/taxonomy")
async def get_taxonomy():
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, name, parent_id, is_active FROM document_taxonomy ORDER BY name ASC")
    rows = c.fetchall()
    conn.close()
    return {"taxonomy": [dict(r) for r in rows]}

@app.post("/api/taxonomy")
async def create_taxonomy(req: TaxonomyCreate, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    try:
        c.execute("INSERT INTO document_taxonomy (name, parent_id) VALUES (?, ?)", (req.name.strip(), req.parent_id))
        conn.commit()
        new_id = c.lastrowid
        return {"id": new_id, "message": "Berhasil menambahkan taksonomi"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Taksonomi dengan nama tersebut sudah ada.")
    finally:
        conn.close()

@app.put("/api/taxonomy/{tax_id}")
async def update_taxonomy(tax_id: int, req: TaxonomyUpdate, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    try:
        if req.name is not None:
            # Update name in taxonomy
            c.execute("SELECT name FROM document_taxonomy WHERE id = ?", (tax_id,))
            old_row = c.fetchone()
            if not old_row:
                raise HTTPException(status_code=404, detail="Taksonomi tidak ditemukan.")
            old_name = old_row[0]
            new_name = req.name.strip()
            
            c.execute("UPDATE document_taxonomy SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_name, tax_id))
            
            # Cascade rename in regulations table
            if old_name != new_name:
                c.execute("UPDATE regulations SET jenis = ? WHERE jenis = ?", (new_name, old_name))
        
        if req.is_active is not None:
            c.execute("UPDATE document_taxonomy SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (1 if req.is_active else 0, tax_id))
            
        conn.commit()
        return {"message": "Berhasil memperbarui taksonomi"}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Nama taksonomi tersebut sudah ada.")
    finally:
        conn.close()

@app.delete("/api/taxonomy/{tax_id}")
async def delete_taxonomy(tax_id: int, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # Check if in use
    c.execute("SELECT name FROM document_taxonomy WHERE id = ?", (tax_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Taksonomi tidak ditemukan.")
        
    tax_name = row[0]
    c.execute("SELECT COUNT(*) FROM regulations WHERE jenis = ?", (tax_name,))
    in_use = c.fetchone()[0] > 0
    
    if in_use:
        # Just deactivate it instead of deleting
        c.execute("UPDATE document_taxonomy SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (tax_id,))
        conn.commit()
        conn.close()
        return {"message": "Taksonomi dinonaktifkan karena sedang digunakan."}
    else:
        c.execute("DELETE FROM document_taxonomy WHERE id = ?", (tax_id,))
        conn.commit()
        conn.close()
        return {"message": "Taksonomi berhasil dihapus secara permanen."}
"""

if "@app.get(\"/api/taxonomy\")" not in content:
    # Insert right before FR-31 System Monitoring section
    if "# \ud83d\udee0\ufe0f FR-31: System Monitoring" in content:
        content = content.replace("# \ud83d\udee0\ufe0f FR-31: System Monitoring", api_routes + "\n# \ud83d\udee0\ufe0f FR-31: System Monitoring")

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("Backend DB and API patches applied.")
