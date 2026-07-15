import re

FILE_PATH = r"c:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\api\routers\repository.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

correct_function = """@router.delete("/repository/document/{doc_id}")
async def delete_document(doc_id: str, current_user: dict = Depends(auth.require_role("sekretaris perusahaan"))):
    \"\"\"Deletes a document from the repository.\"\"\"
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    # Check if doc exists
    c.execute("SELECT local_path, judul FROM regulations WHERE id = ?", (doc_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Not Found")
        
    local_path, judul = row
    
    # 1. Delete physical file
    if local_path and os.path.exists(local_path):
        try:
            os.remove(local_path)
        except Exception as e:
            print(f"Error deleting file {local_path}: {e}")
            
    # 2. Delete from ChromaDB
    try:
        from main import get_chroma_collection
        collection = get_chroma_collection()
        collection.delete(where={"reg_id": doc_id})
    except Exception as e:
        print(f"Error deleting from ChromaDB: {e}")
        
    # 3. Delete from Knowledge Graph
    try:
        c.execute("DELETE FROM kg_nodes WHERE doc_id = ?", (doc_id,))
        c.execute("DELETE FROM kg_edges WHERE source_doc_id = ? OR target_doc_id = ?", (doc_id, doc_id))
    except Exception as e:
        print(f"Error deleting from KG: {e}")
        
    # 4. Delete Access Grants
    try:
        c.execute("DELETE FROM access_grants WHERE doc_id = ?", (doc_id,))
    except Exception as e:
        pass
        
    # 5. Delete Document Record
    c.execute("DELETE FROM regulations WHERE id = ?", (doc_id,))
    
    conn.commit()
    conn.close()
    
    # 6. Audit Log
    from main import log_audit
    log_audit(current_user.get("id", ""), "DELETE_DOCUMENT", doc_id, f"Menghapus dokumen: {judul}")
 
    return {"message": "Dokumen berhasil dihapus."}
"""

# Replace everything from the router.delete line onwards
pattern = r"@router\.delete\([^\n]*\nasync def delete_document.*$"
new_content = re.sub(pattern, correct_function, content, flags=re.DOTALL)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(new_content)
    
print("Successfully fixed delete_document in repository.py!")
