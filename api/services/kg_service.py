"""Knowledge Graph extraction & retrieval.

Moved from api/main.py during the Phase 2 refactor. LLM calls go through
services.llm_client (same implementation that previously lived in main.py).

Migration M3 (cutover): the KG tables live in PostgreSQL; the sqlite3
upserts became ON CONFLICT statements and retrieval goes through
services.pg_service.
"""
import os

from services import pg_service
from services.llm_client import call_glm, extract_json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def extract_and_store_graph(doc_id: str, full_text: str, nomor: str, judul: str, jenis: str):
    """Use LLM to extract entities & relationships from a document, then upsert into KG tables."""
    # FR-30: Fetch Exclusions
    exclusions = []
    try:
        rows = pg_service.query("SELECT entity_name FROM kg_exclusions")
        exclusions = [r["entity_name"] for r in rows]
    except Exception as e:
        print(f"[KG] Warning: Failed to fetch exclusions: {e}")

    exclusion_text = ""
    if exclusions:
        exclusion_list_str = ", ".join(exclusions)
        exclusion_text = f"\n6. DILARANG KERAS mengekstrak entitas berikut ini (Abaikan mereka sepenuhnya): {exclusion_list_str}."

    snippet = full_text[:8000]  # Increased from 3500 to capture definitions (Pasal 1) and core body
    prompt = [
        {"role": "system", "content": (
            "Kamu adalah ekstraktor entitas hukum level ahli. Baca teks peraturan di bawah ini dan ekstrak "
            "entitas serta hubungan hukumnya ke dalam bentuk JSON murni. "
            "Format JSON yang diharapkan:\n"
            "{\"entitas\": [{\"id\": \"string unik\", \"label\": \"nama tampilan\", \"type\": \"entitas|topik\"}], "
            "\"relasi\": [{\"source\": \"id sumber\", \"target\": \"id target\", \"rel\": \"MENCABUT|MENGUBAH|MERUJUK|MENGATUR|DITERBITKAN_OLEH\"}]}\n"
            "Aturan Ekstraksi Kritis:\n"
            "1. Untuk type='entitas': Selalu ekstrak lembaga penerbit (e.g. OJK, Kemnaker, BI, Kemenkeu, Presiden).\n"
            "2. Untuk type='topik': Sangat penting untuk mendeteksi topik terkait skenario NDA dan PKS! Jika teks mengandung unsur 'Kerahasiaan', 'Data Pribadi', 'Keamanan Informasi', 'Rahasia Dagang', ekstrak sebagai topik NDA. Jika mengandung 'Perjanjian', 'Kontrak', 'Kemitraan', 'Vendor', 'Pengadaan', ekstrak sebagai topik PKS.\n"
            "3. Selalu buat relasi DITERBITKAN_OLEH dari regulasi ini ke lembaga penerbitnya.\n"
            "4. Gunakan nomor regulasi resmi (misal POJK-12-2023) sebagai ID untuk target relasi MERUJUK/MENGUBAH.\n"
            "5. Jangan batasi jumlah ekstraksi. Ekstrak SEMUA entitas, topik relevan, dan regulasi terkait yang ada dalam teks untuk membangun Knowledge Graph yang padat dan komprehensif."
            f"{exclusion_text}"
        )},
        {"role": "user", "content": f"Regulasi: {jenis} Nomor {nomor}\nJudul: {judul}\n\nTeks:\n{snippet}"}
    ]
    try:
        raw = call_glm(prompt, temperature=0.0, timeout=45)
        # (Bugfix M3: legacy only stripped a LEADING ``` fence. The current
        # LLM (llama-4-maverick via MODEL_BASE_URL) sometimes prefixes a prose
        # preamble -- "Berikut adalah ...:\n```json\n{...}\n```" -- after which
        # json.loads failed and every KG extraction became a silent no-op.
        # M4: the fence-strip + outermost-span fallback now lives in the shared
        # llm_client.extract_json used by every JSON-parse site. See bug_reports.md.)
        data = extract_json(raw)
        
        # FR-30 Post-processing: remove excluded entities
        if exclusions:
            exc_lower = {e.lower() for e in exclusions}
            # Filter nodes
            valid_entities = []
            excluded_ids = set()
            for ent in data.get("entitas", []):
                if ent.get("label", "").lower() in exc_lower:
                    excluded_ids.add(ent.get("id"))
                else:
                    valid_entities.append(ent)
            data["entitas"] = valid_entities
            
            # Filter edges referencing excluded nodes
            valid_relations = []
            for rel in data.get("relasi", []):
                if rel.get("source") not in excluded_ids and rel.get("target") not in excluded_ids:
                    valid_relations.append(rel)
            data["relasi"] = valid_relations

    except Exception as e:
        print(f"[KG] LLM extraction failed for {nomor}: {e}")
        return

    try:
        # Single transaction, same as the legacy sqlite commit-at-end flow.
        with pg_service.get_conn() as conn:
            # Upsert the regulation itself as a node. INSERT OR REPLACE on
            # sqlite deleted+reinserted the row (refreshing created_at); the
            # explicit created_at = NOW() keeps that behaviour.
            reg_node_id = f"{jenis}-{nomor}".replace(" ", "-")[:80]
            conn.execute(
                "INSERT INTO kg_nodes (id, label, type, doc_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, "
                "type = EXCLUDED.type, doc_id = EXCLUDED.doc_id, created_at = NOW()",
                (reg_node_id, f"{jenis} {nomor}", "regulasi", doc_id))

            # Upsert extracted entities/topics
            for ent in data.get("entitas", []):
                ent_id = str(ent.get("id", "")).strip()[:80]
                ent_label = str(ent.get("label", ent_id)).strip()[:120]
                ent_type = str(ent.get("type", "topik")).strip()
                if ent_id:
                    conn.execute(
                        "INSERT INTO kg_nodes (id, label, type, doc_id) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (ent_id, ent_label, ent_type, None))

            # Insert edges
            for rel in data.get("relasi", []):
                src = str(rel.get("source", "")).strip()[:80]
                tgt = str(rel.get("target", "")).strip()[:80]
                relation = str(rel.get("rel", "MERUJUK")).strip()[:40]
                if src and tgt and src != tgt:
                    # Replace regulation self-reference with the canonical reg_node_id
                    if src == nomor or src == f"{jenis} {nomor}":
                        src = reg_node_id
                    if tgt == nomor or tgt == f"{jenis} {nomor}":
                        tgt = reg_node_id
                    conn.execute(
                        "INSERT INTO kg_edges (source_id, target_id, relation, doc_id) "
                        "VALUES (%s, %s, %s, %s)",
                        (src, tgt, relation, doc_id))

        print(f"[KG] Stored graph for {nomor}: {len(data.get('entitas',[]))} entities, {len(data.get('relasi',[]))} edges")
    except Exception as e:
        print(f"[KG] DB write failed for {nomor}: {e}")

def retrieve_graph_contexts(query: str, current_user: dict, max_nodes=10) -> str:
    """FR-16: Queries the PostgreSQL Knowledge Graph based on query keywords and returns a formatted graph string."""
    import re

    # 1. Clean query to extract keywords
    stopwords = {"apa", "siapa", "kapan", "dimana", "mengapa", "bagaimana", "dan", "atau", "di", "ke", "dari", "yang", "untuk", "dengan", "tentang", "terkait", "saja", "apakah"}
    words = re.findall(r'\b\w+\b', query.lower())
    keywords = [w for w in words if w not in stopwords and len(w) > 3]
    
    if not keywords:
        return ""

    try:
        # 2. Find matching nodes (the % wildcards live in the parameter
        # values, so the SQL text itself needs no escaping)
        query_conditions = " OR ".join(["label LIKE %s" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]

        matched_nodes = pg_service.query(
            f"SELECT id, label, type FROM kg_nodes WHERE {query_conditions} LIMIT {int(max_nodes)}",
            params)

        if not matched_nodes:
            return ""

        matched_node_ids = [row["id"] for row in matched_nodes]

        # 3. Find 1-degree connections (labels aliased: dict rows cannot
        # carry two columns named "label")
        placeholders = ",".join(["%s"] * len(matched_node_ids))
        edges = pg_service.query(f"""
            SELECT e.source_id, n1.label AS src_label, e.relation, e.target_id, n2.label AS tgt_label 
            FROM kg_edges e
            LEFT JOIN kg_nodes n1 ON e.source_id = n1.id
            LEFT JOIN kg_nodes n2 ON e.target_id = n2.id
            WHERE e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders})
            LIMIT 40
        """, matched_node_ids + matched_node_ids)

        if not edges:
            return ""

        # 4. Format into natural text
        graph_text = "STRUKTUR KNOWLEDGE GRAPH (Entitas dan Relasi yang relevan dengan pertanyaan):\n"
        for e in edges:
            s_lbl = e["src_label"] or e["source_id"]
            t_lbl = e["tgt_label"] or e["target_id"]
            graph_text += f"- [{s_lbl}] {e['relation']} [{t_lbl}]\n"

        return graph_text
    except Exception as e:
        print(f"Graph retrieval error: {e}")
        return ""
