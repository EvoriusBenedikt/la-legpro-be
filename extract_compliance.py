import sys
import os

def extract_compliance():
    lines = open('api/main.py', encoding='utf-8').readlines()
    
    with open('api/routers/compliance.py', 'w', encoding='utf-8') as f:
        f.write('from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks\n')
        f.write('import sqlite3, os, json, time, re\n')
        f.write('from typing import List, Optional, Any\n')
        f.write('from pydantic import BaseModel\n')
        f.write('import auth\n')
        f.write('import chromadb\n')
        f.write('from services.llm_client import call_glm\n')
        f.write('from main import extract_pasal_items, get_chroma_collection\n\n')
        f.write('router = APIRouter(prefix="/api", tags=["compliance"])\n\n')
        f.write('BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n\n')
        
        f.write('class AnalyzeRequest(BaseModel):\n    doc_id: int\n\n')
        f.write('class PasalAnalyzeRequest(BaseModel):\n    doc_id: int\n    pasals: List[str]\n\n')
        
        start_idx = 0
        for i, line in enumerate(lines):
            if '@app.post("/api/upload")' in line:
                start_idx = i
                break
                
        # Copy everything from /api/upload to the end, EXCEPT the if __name__ block
        end_idx = len(lines)
        for i in range(start_idx, len(lines)):
            if 'if __name__ == "__main__":' in lines[i]:
                end_idx = i
                break
                
        for line in lines[start_idx:end_idx]:
            line = line.replace('@app.post("/api/upload"', '@router.post("/upload"')
            line = line.replace('@app.post("/api/analyze"', '@router.post("/analyze"')
            line = line.replace('@app.post("/api/analyze-pasals"', '@router.post("/analyze-pasals"')
            line = line.replace('@app.post("/api/check-compliance"', '@router.post("/check-compliance"')
            f.write(line)

    print(f"Extraction successful: compliance({start_idx}-{end_idx})")

if __name__ == "__main__":
    extract_compliance()
