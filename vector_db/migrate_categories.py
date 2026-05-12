"""
migrate_categories.py
Assigns doc_category="PKS" to all existing metadata in the ojk_regulations ChromaDB collection
that don't already have a doc_category.
"""

import chromadb
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

def main():
    print(f"Connecting to ChromaDB at {CHROMA_DIR}...")
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    
    try:
        col = client.get_collection(name="ojk_regulations")
    except ValueError:
        print("Collection 'ojk_regulations' does not exist.")
        return
        
    total_count = col.count()
    print(f"Total documents in collection: {total_count}")
    
    # Process in batches to avoid memory issues and SQLite limits
    batch_size = 5000
    updated_count = 0
    
    for offset in range(0, total_count, batch_size):
        print(f"Fetching records {offset} to {offset + batch_size}...")
        results = col.get(limit=batch_size, offset=offset, include=["metadatas"])
        
        ids_to_update = []
        metadatas_to_update = []
        
        for doc_id, meta in zip(results['ids'], results['metadatas']):
            if meta and "doc_category" not in meta:
                new_meta = meta.copy()
                new_meta["doc_category"] = "PKS"
                
                ids_to_update.append(doc_id)
                metadatas_to_update.append(new_meta)
                
        if ids_to_update:
            print(f"Updating batch of {len(ids_to_update)} records...")
            col.update(ids=ids_to_update, metadatas=metadatas_to_update)
            updated_count += len(ids_to_update)
            
    print(f"Migration complete. Updated {updated_count} out of {total_count} documents with doc_category='PKS'.")

if __name__ == "__main__":
    main()
