from fastapi import APIRouter, Depends, BackgroundTasks
import json
from pydantic import BaseModel
import auth
from services import pg_service
from services.llm_client import call_glm

router = APIRouter(prefix="/api/knowledge-graph", tags=["kg"])

class ScenarioRequest(BaseModel):
    scenario: str

@router.get("")
async def get_knowledge_graph(
    search: str = "",
    node_type: str = "",
    current_user: dict = Depends(auth.require_role("manajer"))
):
    """Returns all KG nodes and edges for the graph visualizer."""
    node_query = "SELECT id, label, type, doc_id FROM kg_nodes WHERE 1=1"
    params = []
    if search:
        node_query += " AND label LIKE %s"
        params.append(f"%{search}%")
    if node_type:
        node_query += " AND type = %s"
        params.append(node_type)
    node_query += " LIMIT 500"

    nodes = pg_service.query(node_query, tuple(params))

    node_ids = {n["id"] for n in nodes}

    # Only return edges where both endpoints are in the node set
    all_edges = pg_service.query(
        "SELECT id, source_id, target_id, relation, doc_id FROM kg_edges LIMIT 2000")
    edges = [e for e in all_edges if e["source_id"] in node_ids and e["target_id"] in node_ids]

    total_nodes = pg_service.query_one("SELECT COUNT(*) AS n FROM kg_nodes")["n"]
    total_edges = pg_service.query_one("SELECT COUNT(*) AS n FROM kg_edges")["n"]

    return {"nodes": nodes, "edges": edges, "total_nodes": total_nodes, "total_edges": total_edges}

@router.get("/export")
async def export_knowledge_graph(
    format: str = "json",
    current_user: dict = Depends(auth.require_role("manajer"))
):
    """Exports KG nodes, edges, and document metadata (FR-28)."""
    import io, csv, zipfile
    from fastapi.responses import JSONResponse, StreamingResponse
    nodes = pg_service.query("SELECT id, label, type, doc_id FROM kg_nodes")

    edges = pg_service.query("SELECT id, source_id, target_id, relation, doc_id FROM kg_edges")

    documents = pg_service.query(
        "SELECT id, domain, jenis, nomor, judul, status, sektor, detail_url, "
        "download_url, local_path, klasifikasi FROM regulations")

    if format.lower() == "csv":
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            if nodes:
                node_buffer = io.StringIO()
                writer = csv.DictWriter(node_buffer, fieldnames=nodes[0].keys())
                writer.writeheader()
                writer.writerows(nodes)
                zip_file.writestr("nodes.csv", node_buffer.getvalue())
            if edges:
                edge_buffer = io.StringIO()
                writer = csv.DictWriter(edge_buffer, fieldnames=edges[0].keys())
                writer.writeheader()
                writer.writerows(edges)
                zip_file.writestr("edges.csv", edge_buffer.getvalue())
            if documents:
                doc_buffer = io.StringIO()
                writer = csv.DictWriter(doc_buffer, fieldnames=documents[0].keys())
                writer.writeheader()
                writer.writerows(documents)
                zip_file.writestr("documents.csv", doc_buffer.getvalue())

        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer, 
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=legal_data_export.zip"}
        )
    
    return JSONResponse(
        content={"nodes": nodes, "edges": edges, "documents": documents},
        headers={"Content-Disposition": "attachment; filename=legal_data_export.json"}
    )

def _rebuild_kg_batch():
    from services.kg_service import extract_and_store_graph
    from services.embed_service import embed_query
    """Background worker: rebuilds KG by pulling text from the PG chunks store.
    Used for documents ingested via the scraper that have no local PDF files.
    (Migration M3: the per-document Chroma semantic lookup became a pgvector
    nearest-neighbour query filtered to that doc_id.)
    """
    import time
    start_time_ts = time.time()
    
    history_id = None
    try:
        rows = pg_service.execute_returning(
            "INSERT INTO kg_rebuild_history (start_time, status) "
            "VALUES (NOW(), 'RUNNING') RETURNING id")
        history_id = rows[0]["id"] if rows else None
    except Exception:
        pass
    
    nodes_before = pg_service.query_one("SELECT COUNT(*) AS n FROM kg_nodes")["n"]
    edges_before = pg_service.query_one("SELECT COUNT(*) AS n FROM kg_edges")["n"]
    # id::text -- kg_nodes.doc_id is TEXT while regulations.id is int; no
    # params given, so the literal % in LIKE 'Berlaku%' needs no doubling.
    docs = pg_service.query(
        "SELECT id, nomor, jenis, judul FROM regulations "
        "WHERE status LIKE 'Berlaku%' "
        "AND id::text NOT IN (SELECT DISTINCT doc_id FROM kg_nodes "
        "                     WHERE doc_id IS NOT NULL)")

    print(f"[KG Rebuild] Starting chunks-based batch for {len(docs)} documents...")
    success, failed, skipped = 0, 0, 0

    for doc in docs:
        try:
            # Fetch the chunks that belong to this regulation (Migration M3:
            # pgvector nearest-neighbour on the doc identity text -- same
            # query text, filter and n_results=5 as the legacy Chroma call)
            q_text = doc["nomor"] or doc["judul"] or ""
            q_vec = embed_query(q_text)
            chunk_rows = pg_service.query(
                "SELECT text FROM chunks "
                "WHERE doc_id = %s AND embedding IS NOT NULL "
                "ORDER BY embedding <=> %s::vector LIMIT 5",
                (str(doc["id"]), q_vec))
            documents_list = [r["text"] for r in chunk_rows]
            if not documents_list:
                # fallback: try matching by nomor in metadata
                skipped += 1
                continue

            full_text = "\n\n".join(documents_list)
            extract_and_store_graph(
                # str(): kg_nodes.doc_id / kg_edges.doc_id are TEXT; legacy
                # sqlite coerced the int id via column affinity, PG errors on
                # an int param into a TEXT column (same value as legacy '123').
                str(doc["id"]), full_text,
                doc["nomor"] or "", doc["judul"] or "", doc["jenis"] or ""
            )
            success += 1
        except Exception as e:
            print(f"[KG Rebuild] Failed for {doc['nomor']}: {e}")
            failed += 1

    print(f"[KG Rebuild] Done. Success: {success}, Skipped (no chunks): {skipped}, Failed: {failed}")

    try:
        nodes_after = pg_service.query_one("SELECT COUNT(*) AS n FROM kg_nodes")["n"]
        edges_after = pg_service.query_one("SELECT COUNT(*) AS n FROM kg_edges")["n"]
        duration = int(time.time() - start_time_ts)
        if history_id:
            pg_service.execute(
                "UPDATE kg_rebuild_history SET end_time = NOW(), duration_s = %s, "
                "nodes_changed = %s, edges_changed = %s, status = %s WHERE id = %s",
                (duration, nodes_after - nodes_before, edges_after - edges_before,
                 'COMPLETED', history_id))
    except Exception as e:
        print(f"[KG Rebuild] Failed to save history: {e}")


class ScenarioAnalyzeRequest(BaseModel):
    scenario: str

@router.post("/analyze-scenario")
async def analyze_kg_scenario(req: ScenarioAnalyzeRequest):
    """
    Uses LLM to dynamically select which node IDs are relevant to the requested scenario.
    """
    # Fetch all node IDs and labels
    nodes = pg_service.query("SELECT id, label FROM kg_nodes")

    # We only send a subset of data to avoid exceeding context if it's too large,
    # but 1400 nodes is about ~50k chars which is perfectly fine for modern LLMs.
    nodes_str = "\n".join([f"ID: {n['id']} | Label: {n['label']}" for n in nodes])
    
    messages = [
        {"role": "system", "content": "You are an expert legal knowledge graph analyst. Your job is to return a JSON array of Node IDs that are highly relevant to the user's requested scenario. Be strict and only return nodes directly involved with the scenario."},
        {"role": "user", "content": f"Here is the list of all nodes in our knowledge graph:\n\n{nodes_str}\n\nScenario: {req.scenario}\n\nReturn ONLY a JSON array of strings containing the exact IDs of the nodes that are highly relevant to this scenario. Example: [\"node1\", \"node2\"]. Return nothing else."}
    ]
    
    try:
        raw_response = call_glm(messages, temperature=0.1, timeout=90)
        
        # Parse out JSON block
        import re
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
    with pg_service.get_conn() as conn:
        conn.execute("DELETE FROM kg_edges WHERE doc_id = %s", (doc_id,))
        conn.execute("DELETE FROM kg_nodes WHERE doc_id = %s", (doc_id,))
    return {"message": "Data graph untuk dokumen ini telah dihapus."}

