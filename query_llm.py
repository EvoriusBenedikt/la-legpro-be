import os
import sys
import chromadb
import requests
from dotenv import load_dotenv

# Load environment variables
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CHROMA_DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

# We will use your locally installed Ollama with Llama 3!
LLM_API_URL = "http://localhost:11434/api/chat"
MODEL_NAME = "llama3"

def get_chroma_collection():
    client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    collection = client.get_or_create_collection(
        name="ojk_regulations",
        metadata={"hnsw:space": "cosine"}
    )
    return collection

def retrieve_context(query, n_results=5):
    """Searches the Vector DB for the most relevant legal chunks."""
    collection = get_chroma_collection()
    results = collection.query(
        query_texts=[query],
        n_results=n_results
    )
    
    contexts = []
    print("\n--- DOKUMEN YANG DITEMUKAN (SUMBER RAG) ---")
    if results and results['documents']:
        for i in range(len(results['documents'][0])):
            doc = results['documents'][0][i]
            meta = results['metadatas'][0][i]
            
            print(f"[{i+1}] {meta.get('jenis')} Nomor {meta.get('nomor')} ({meta.get('sektor')})")
            print(f"    Snippet: {doc[:150]}...\n")
            
            # Formatting the retrieved context
            context_str = f"SUMBER: {meta.get('jenis')} Nomor {meta.get('nomor')} Sektor {meta.get('sektor')}\n"
            context_str += f"JUDUL: {meta.get('judul')}\n"
            context_str += f"TEKS HUKUM:\n{doc}\n"
            contexts.append(context_str)
            
    return "\n" + ("-"*40) + "\n".join(contexts)

def query_legal_analyzer(user_question):
    """Retrieves context and asks the LLM to answer the legal question."""
    print("1. Retrieving relevant legal context from ChromaDB...")
    context = retrieve_context(user_question)
    
    system_prompt = (
        "Anda adalah AI Legal Analyzer (Asisten Hukum) yang ahli dalam Peraturan Otoritas Jasa Keuangan (OJK) di Indonesia. "
        "Gunakan HANYA konteks hukum yang diberikan di bawah ini untuk menjawab pertanyaan pengguna. "
        "Jawablah dalam Bahasa Indonesia yang formal dan profesional. "
        "Jika jawabannya tidak ada di dalam konteks, katakan dengan tegas bahwa Anda tidak memiliki informasi mengenai aturan tersebut di database saat ini. "
        "Jangan pernah mengarang jawaban. Selalu sebutkan SUMBER (Misalnya: POJK Nomor X Pasal Y) dalam jawaban Anda."
    )
    
    user_prompt = f"PERTANYAAN PENGGUNA:\n{user_question}\n\nKONTEKS HUKUM OJK:\n{context}"
    
    print("2. Generating Answer using Local LLM (Ollama Llama 3)...\n")
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }
    
    try:
        response = requests.post(LLM_API_URL, json=payload)
        response.raise_for_status()
        answer = response.json().get("message", {}).get("content", "")
        print("=== JAWABAN AI LEGAL ANALYZER (LOKAL) ===")
        print(answer)
        print("\n=========================================")
    except Exception as e:
        print(f"Ollama API Error: {e}")
        print("Pastikan Ollama sedang berjalan (buka aplikasi Ollama atau jalankan 'ollama run llama3')!")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Gunakan perintah: python query_llm.py "Pertanyaan hukum Anda disini"')
        sys.exit(1)
        
    question = sys.argv[1]
    query_legal_analyzer(question)
