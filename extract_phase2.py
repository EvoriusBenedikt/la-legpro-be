import sys
import os

def extract_routes():
    lines = open('api/main.py', encoding='utf-8').readlines()
    
    # 1. We will create api/routers/chat.py
    # Lines 1338 to 1424 (chat routes)
    with open('api/routers/chat.py', 'w', encoding='utf-8') as f:
        f.write('from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks\n')
        f.write('import sqlite3, os, json, time, re\n')
        f.write('from typing import List, Optional, Any\n')
        f.write('from pydantic import BaseModel\n')
        f.write('import auth\n')
        f.write('import chromadb\n')
        f.write('from services.llm_client import call_glm\n\n')
        f.write('router = APIRouter(prefix="/api", tags=["chat"])\n\n')
        f.write('BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n\n')
        
        # Pydantic models for chat
        f.write('class ChatMessage(BaseModel):\n    role: str\n    content: str\n\n')
        f.write('class ChatRequest(BaseModel):\n    message: str\n    history: List[ChatMessage] = []\n\n')
        f.write('class ChatResponse(BaseModel):\n    response: str\n    citations: List[dict] = []\n\n')
        f.write('class SessionRequest(BaseModel):\n    title: str\n\n')
        
        # We need the ChromaDB client init since chat uses it
        f.write('try:\n    chroma_client = chromadb.PersistentClient(path=os.path.join(BASE_DIR, "data", "chromadb"))\n')
        f.write('    collection = chroma_client.get_or_create_collection(name="regulations")\n')
        f.write('except Exception as e:\n    print("ChromaDB Error in chat router:", e)\n    collection = None\n\n')

        # Find exact line numbers for chat
        chat_start, chat_end = 0, 0
        for i, line in enumerate(lines):
            if '@app.post("/api/chat"' in line: chat_start = i
            if '@app.get("/api/pdf/{filename}")' in line: 
                chat_end = i
                break
                
        for line in lines[chat_start:chat_end]:
            line = line.replace('@app.post("/api/chat"', '@router.post("/chat"')
            line = line.replace('@app.post("/api/chat-session"', '@router.post("/chat-session"')
            f.write(line)

    # 2. Extract repository.py (includes pdf, repository, grants)
    with open('api/routers/repository.py', 'w', encoding='utf-8') as f:
        f.write('from fastapi import APIRouter, Depends, HTTPException\n')
        f.write('from fastapi.responses import FileResponse\n')
        f.write('import sqlite3, os\n')
        f.write('from pydantic import BaseModel\n')
        f.write('import auth\n\n')
        f.write('router = APIRouter(prefix="/api", tags=["repository"])\n\n')
        f.write('BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n\n')
        
        f.write('class GrantAccessRequest(BaseModel):\n    target_user_id: int\n    expires_in_days: int = 7\n\n')
        
        # Need to capture 1425 down to 1780
        repo_start, repo_end = 0, 0
        for i, line in enumerate(lines):
            if '@app.get("/api/pdf/{filename}")' in line: repo_start = i
            if '@app.get("/api/knowledge-graph")' in line: 
                repo_end = i
                break
        
        for line in lines[repo_start:repo_end]:
            line = line.replace('@app.get("/api/pdf', '@router.get("/pdf')
            line = line.replace('@app.get("/api/repository', '@router.get("/repository')
            line = line.replace('@app.post("/api/documents', '@router.post("/documents')
            line = line.replace('@app.delete("/api/repository', '@router.delete("/repository')
            f.write(line)
            
    # 3. Extract knowledge_graph.py
    with open('api/routers/knowledge_graph.py', 'w', encoding='utf-8') as f:
        f.write('from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks\n')
        f.write('import sqlite3, os, json\n')
        f.write('from pydantic import BaseModel\n')
        f.write('import auth\n\n')
        f.write('router = APIRouter(prefix="/api/knowledge-graph", tags=["kg"])\n\n')
        f.write('BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n\n')
        
        f.write('class ScenarioRequest(BaseModel):\n    scenario: str\n\n')
        
        kg_start, kg_end = 0, 0
        for i, line in enumerate(lines):
            if '@app.get("/api/knowledge-graph")' in line: kg_start = i
            if '@app.get("/api/admin/dashboard")' in line or '@app.delete("/api/repository/failed")' in line: 
                kg_end = i
                break
                
        for line in lines[kg_start:kg_end]:
            line = line.replace('@app.get("/api/knowledge-graph"', '@router.get("")')
            line = line.replace('@app.post("/api/knowledge-graph', '@router.post("')
            line = line.replace('@app.delete("/api/knowledge-graph', '@router.delete("')
            f.write(line)
            
    print(f"Extraction successful: chat({chat_start}-{chat_end}), repo({repo_start}-{repo_end}), kg({kg_start}-{kg_end})")

if __name__ == "__main__":
    extract_routes()
