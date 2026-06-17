import os

filepath = os.path.join("api", "main.py")
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Define Pydantic model near the top or just before the endpoint
new_model = """class ScenarioAnalyzeRequest(BaseModel):
    scenario: str

@app.post("/api/knowledge-graph/analyze-scenario")
async def analyze_kg_scenario(req: ScenarioAnalyzeRequest):
    \"\"\"
    Uses LLM to dynamically select which node IDs are relevant to the requested scenario.
    \"\"\"
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
    nodes_str = "\\n".join([f"ID: {n[0]} | Label: {n[1]}" for n in nodes])
    
    messages = [
        {"role": "system", "content": "You are an expert legal knowledge graph analyst. Your job is to return a JSON array of Node IDs that are highly relevant to the user's requested scenario. Be strict and only return nodes directly involved with the scenario."},
        {"role": "user", "content": f"Here is the list of all nodes in our knowledge graph:\\n\\n{nodes_str}\\n\\nScenario: {req.scenario}\\n\\nReturn ONLY a JSON array of strings containing the exact IDs of the nodes that are highly relevant to this scenario. Example: [\\"node1\\", \\"node2\\"]. Return nothing else."}
    ]
    
    try:
        raw_response = call_glm(messages, temperature=0.1, timeout=90)
        
        # Parse out JSON block
        import re
        import json
        match = re.search(r'\\[.*?\\]', raw_response, re.DOTALL)
        if match:
            node_ids = json.loads(match.group(0))
            return {"status": "success", "matchedNodeIds": node_ids}
        else:
            return {"status": "error", "matchedNodeIds": []}
    except Exception as e:
        print(f"LLM Scenario Error: {e}")
        return {"status": "error", "matchedNodeIds": []}

@app.post("/api/knowledge-graph/rebuild")"""

content = content.replace('@app.post("/api/knowledge-graph/rebuild")', new_model)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("Backend LLM patch applied successfully.")
