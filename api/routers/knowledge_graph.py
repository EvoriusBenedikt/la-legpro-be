from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
import sqlite3, os, json
from pydantic import BaseModel
import auth
from services.llm_client import call_glm

router = APIRouter(prefix="/api/knowledge-graph", tags=["kg"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class ScenarioRequest(BaseModel):
    scenario: str

@router.get("")
async def get_knowledge_graph(
    search: str = "",
    node_type: str = "",
    current_user: dict = Depends(auth.require_role("manajer"))
):
    """Returns all KG nodes and edges for the graph visualizer."""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    node_query = "SELECT id, label, type, doc_id FROM kg_nodes WHERE 1=1"
    params = []
    if search:
        node_query += " AND label LIKE ?"
        params.append(f"%{search}%")
    if node_type:
        node_query += " AND type = ?"
        params.append(node_type)
    node_query += " LIMIT 500"

    c.execute(node_query, params)
    nodes = [dict(r) for r in c.fetchall()]

    node_ids = {n["id"] for n in nodes}

    # Only return edges where both endpoints are in the node set
    c.execute("SELECT id, source_id, target_id, relation, doc_id FROM kg_edges LIMIT 2000")
    all_edges = c.fetchall()
    edges = [dict(e) for e in all_edges if e["source_id"] in node_ids and e["target_id"] in node_ids]

    c.execute("SELECT COUNT(*) FROM kg_nodes")
    total_nodes = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM kg_edges")
    total_edges = c.fetchone()[0]

    conn.close()
    return {"nodes": nodes, "edges": edges, "total_nodes": total_nodes, "total_edges": total_edges}


def _rebuild_kg_batch():
    from api.main import get_chroma_collection, extract_and_store_graph
    """Background worker: rebuilds KG by pulling text from ChromaDB chunks.
    Used for documents ingested via the scraper that have no local PDF files.
    """
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, nomor, jenis, judul FROM regulations WHERE status LIKE 'Berlaku%' AND id NOT IN (SELECT DISTINCT doc_id FROM kg_nodes WHERE doc_id IS NOT NULL)")
    docs = c.fetchall()
    conn.close()

    collection = get_chroma_collection()
    print(f"[KG Rebuild] Starting ChromaDB-based batch for {len(docs)} documents...")
    success, failed, skipped = 0, 0, 0

    for doc in docs:
        try:
            # Query ChromaDB for chunks that belong to this regulation
            results = collection.query(
                query_texts=[doc["nomor"] or doc["judul"] or ""],
                n_results=5,
                where={"reg_id": str(doc["id"])}
            )
            documents_list = results.get("documents", [[]])[0]
            if not documents_list:
                # fallback: try matching by nomor in metadata
                skipped += 1
                continue

            full_text = "\n\n".join(documents_list)
            extract_and_store_graph(
                doc["id"], full_text,
                doc["nomor"] or "", doc["judul"] or "", doc["jenis"] or ""
            )
            success += 1
        except Exception as e:
            print(f"[KG Rebuild] Failed for {doc['nomor']}: {e}")
            failed += 1

    print(f"[KG Rebuild] Done. Success: {success}, Skipped (no chunks): {skipped}, Failed: {failed}")


class ScenarioAnalyzeRequest(BaseModel):
    scenario: str

@router.post("/analyze-scenario")
async def analyze_kg_scenario(req: ScenarioAnalyzeRequest):
    """
    Uses LLM to dynamically select which node IDs are relevant to the requested scenario.
    """
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    
    # Fetch all node IDs and labels
    c.execute("SELECT id, label FROM kg_nodes")
    nodes = c.fetchall()
    conn.close()
    
    # We only send a subset of data to avoid exceeding context if it's too large,
    # but 1400 nodes is about ~50k chars which is perfectly fine for modern LLMs.
    nodes_str = "\n".join([f"ID: {n[0]} | Label: {n[1]}" for n in nodes])
    
    messages = [
        {"role": "system", "content": "You are an expert legal knowledge graph analyst. Your job is to return a JSON array of Node IDs that are highly relevant to the user's requested scenario. Be strict and only return nodes directly involved with the scenario."},
        {"role": "user", "content": f"Here is the list of all nodes in our knowledge graph:\n\n{nodes_str}\n\nScenario: {req.scenario}\n\nReturn ONLY a JSON array of strings containing the exact IDs of the nodes that are highly relevant to this scenario. Example: [\"node1\", \"node2\"]. Return nothing else."}
    ]
    
    try:
        raw_response = call_glm(messages, temperature=0.1, timeout=90)
        
        # Parse out JSON block
        import re
        import json
        match = re.search(r'\[.*?\]', raw_response, re.DOTALL)
        if match:
            node_ids = json.loads(match.group(0))
            return {"status": "success", "matchedNodeIds": node_ids}
        else:
            return {"status": "error", "matchedNodeIds": []}
    except Exception as e:
        print(f"LLM Scenario Error: {e}")
        return {"status": "error", "matchedNodeIds": []}

@router.post("/rebuild")
async def rebuild_knowledge_graph(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(auth.require_role("admin"))
):
    """Admin-only: triggers batch KG rebuild for all existing documents."""
    background_tasks.add_task(_rebuild_kg_batch)
    return {"message": "Rebuild dimulai di background. Proses ini bisa memakan waktu 30-60 menit."}


@router.delete("/document/{doc_id}")
async def delete_doc_from_graph(
    doc_id: str,
    current_user: dict = Depends(auth.require_role("manajer"))
):
    """Removes all KG nodes and edges created by a specific document."""
    import sqlite3
    db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
    conn = sqlite3.connect(db_path, timeout=30.0)
    c = conn.cursor()
    c.execute("DELETE FROM kg_edges WHERE doc_id = ?", (doc_id,))
    c.execute("DELETE FROM kg_nodes WHERE doc_id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return {"message": "Data graph untuk dokumen ini telah dihapus."}

    # Check if document exists
    c.execute("SELECT id, local_path FROM regulations WHERE id = ?", (doc_id,))
    doc = c.fetchone()
    if not doc:
        conn.close()
        raise HTTPException(status_code=404, detail="Dokumen tidak ditemukan")
    
    local_path = doc[1]
    
    # Delete physical file
    if local_path and os.path.exists(local_path):
        try:
            os.remove(local_path)
        except Exception as e:
            print(f"Failed to delete file {local_path}: {e}")
            
    # Delete from DB
    c.execute("DELETE FROM regulations WHERE id = ?", (doc_id,))
    c.execute("DELETE FROM access_grants WHERE doc_id = ?", (doc_id,))
    c.execute("DELETE FROM kg_edges WHERE doc_id = ?", (doc_id,))
    c.execute("DELETE FROM kg_nodes WHERE doc_id = ?", (doc_id,))
    
    conn.commit()
    conn.close()
    
    # Delete from ChromaDB
    try:
        collection = get_chroma_collection()
        collection.delete(where={"reg_id": doc_id})
    except Exception as e:
        print(f"Failed to delete from ChromaDB: {e}")
        
    return {"message": "Dokumen berhasil dihapus"}

