import sys
import os

lines = open('api/main.py', encoding='utf-8').readlines()

with open('api/routers/admin.py', 'w', encoding='utf-8') as f:
    f.write('from fastapi import APIRouter, Depends, HTTPException\n')
    f.write('import sqlite3\nimport os\nfrom typing import List\nfrom pydantic import BaseModel\n')
    f.write('import auth\n\n')
    f.write('router = APIRouter(prefix="/api/admin", tags=["admin"])\n\n')
    f.write('BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n\n')
    f.write('class KGExclusion(BaseModel):\n    entity_name: str\n\n')
    
    # Dashboard route (1935 to 2025)
    for line in lines[1935:2025]:
        line = line.replace('@app.get("/api/admin', '@router.get("')
        f.write(line)
    
    # Exclusions routes (2736 to 2809)
    for line in lines[2736:2809]:
        line = line.replace('@app.get("/api/admin', '@router.get("')
        line = line.replace('@app.post("/api/admin', '@router.post("')
        line = line.replace('@app.delete("/api/admin', '@router.delete("')
        f.write(line)

with open('api/routers/engineer.py', 'w', encoding='utf-8') as f:
    f.write('from fastapi import APIRouter, Depends, HTTPException\n')
    f.write('import sqlite3\nimport os\nimport psutil\nfrom typing import Dict, Any\n')
    f.write('import auth\n\n')
    f.write('router = APIRouter(prefix="/api/engineer", tags=["engineer"])\n\n')
    f.write('BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n\n')
    
    # Engineer routes (2809 to 2901)
    for line in lines[2809:2901]:
        line = line.replace('@app.get("/api/engineer', '@router.get("')
        line = line.replace('@app.post("/api/engineer', '@router.post("')
        f.write(line)
