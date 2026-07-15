import re
import os

FILE_PATH = r"c:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\api\main.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

new_logic = """    # Fallback to Hybrid Retrieval (Dense + BM25 Sparse) & RRF
    chunk_data = {}
    dense_ranks = {}
    sparse_ranks = {}
    
    # --- 1. Dense Retrieval (ChromaDB) ---
    dense_res = collection.query(
        query_texts=[query],
        n_results=overfetch_n,
        where=where_clause
    )
    
    dense_rank = 1
    if dense_res and dense_res['documents']:
        for i in range(len(dense_res['documents'][0])):
            meta = dense_res['metadatas'][0][i]
            reg_id = meta.get("reg_id")
            
            if not is_allowed(reg_id):
                continue
                
            chunk_id = dense_res['ids'][0][i]
            base_doc = dense_res['documents'][0][i]
            window_doc = meta.get('window_context', base_doc)
            
            dense_ranks[chunk_id] = dense_rank
            chunk_data[chunk_id] = {
                "id": chunk_id,
                "text": window_doc,
                "jenis": meta.get('jenis', ''),
                "nomor": meta.get('nomor', ''),
                "sektor": meta.get('sektor', ''),
                "judul": meta.get('judul', '')
            }
            dense_rank += 1

    # --- 2. Sparse Retrieval (SQLite FTS5 BM25) ---
    import string
    # Clean query for FTS MATCH syntax to avoid OperationalErrors
    safe_query = query.replace('"', '').replace("'", "")
    safe_query = " OR ".join([word for word in safe_query.split() if len(word) > 2])
    
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        c = conn.cursor()
        
        # We fetch extra because we still need to filter by is_allowed
        c.execute(\"\"\"
            SELECT chunk_id, doc_id, text, window_context
            FROM chunks_fts 
            WHERE chunks_fts MATCH ? 
            ORDER BY rank 
            LIMIT ?
        \"\"\", (safe_query, overfetch_n * 2))
        
        sparse_rank = 1
        for row in c.fetchall():
            chunk_id, doc_id, text, window_context = row
            if not is_allowed(doc_id):
                continue
                
            sparse_ranks[chunk_id] = sparse_rank
            
            # If Chroma didn't find this chunk, we need to populate its data
            if chunk_id not in chunk_data:
                # To get metadata like 'judul', we query the main table
                c.execute("SELECT judul, nomor, jenis, sektor FROM regulations WHERE id = ?", (doc_id,))
                reg_row = c.fetchone()
                if reg_row:
                    judul, nomor, jenis, sektor = reg_row
                    chunk_data[chunk_id] = {
                        "id": chunk_id,
                        "text": window_context if window_context else text,
                        "jenis": jenis or '',
                        "nomor": nomor or '',
                        "sektor": sektor or '',
                        "judul": judul or ''
                    }
            sparse_rank += 1
            
        conn.close()
    except Exception as e:
        print(f"BM25 Sparse Retrieval Error: {e}")

    # --- 3. Reciprocal Rank Fusion (RRF) ---
    k = 60
    rrf_scores = {}
    
    unique_chunk_ids = set(dense_ranks.keys()).union(set(sparse_ranks.keys()))
    for cid in unique_chunk_ids:
        score = 0.0
        if cid in dense_ranks:
            score += 1.0 / (k + dense_ranks[cid])
        if cid in sparse_ranks:
            score += 1.0 / (k + sparse_ranks[cid])
        rrf_scores[cid] = score
        
    # Sort by descending RRF score
    ranked_chunks = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Build final context list
    for cid, score in ranked_chunks[:n_results]:
        if cid in chunk_data:
            contexts.append(chunk_data[cid])
            
    return contexts"""

pattern = r"\s*# Fallback to standard chunk-based RAG.*return contexts"
new_content = re.sub(pattern, "\n" + new_logic, content, flags=re.DOTALL)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(new_content)
    
print("Successfully patched main.py for Hybrid Retrieval!")
