import os
import time
import chromadb
import requests
import json
from termcolor import colored

from dotenv import load_dotenv

# --- Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CHROMA_DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

# Using the GLM API from .env
GLM_BASE_URL = os.getenv("GLM_BASE_URL")
GLM_API_KEY = os.getenv("GLM_API_KEY")
MODEL_NAME = os.getenv("GLM_MODEL")

OLLAMA_URL = f"{GLM_BASE_URL}/chat/completions"

# --- Test Data (Golden Questions) ---
# Format: {"question": "...", "expected_keywords": ["..."]}
TEST_SUITE = [
    {
        "question": "Apa itu P2P Lending menurut OJK?",
        "expected_keywords": ["layanan pendanaan", "teknologi informasi", "lpmubti"]
    },
    {
        "question": "Siapa Menteri Keuangan Republik Indonesia saat ini?",
        # The AI should fail or say "I don't know" since it's not a regulation
        "expected_keywords": ["tidak", "informasi", "konteks"]
    },
    {
        "question": "Apa sanksi administratif jika melanggar ketentuan perlindungan konsumen?",
        "expected_keywords": ["peringatan", "tertulis", "denda", "pencabutan"]
    },
    {
        "question": "Berapa modal disetor minimum untuk mendirikan Bank Umum berbadan hukum Perseroan Terbatas (PT)?",
        "expected_keywords": ["3 triliun", "triliun", "10 triliun"]
    },
    {
        "question": "Apakah Bank Umum Syariah diperbolehkan melakukan kegiatan usaha perasuransian secara langsung?",
        "expected_keywords": ["tidak", "dilarang", "asuransi"]
    },
    {
        "question": "Berapa batas waktu maksimal bagi Pelaku Usaha Jasa Keuangan (PUJK) untuk menyelesaikan pengaduan nasabah?",
        "expected_keywords": ["20 hari", "dua puluh hari", "kerja"]
    },
    {
        "question": "Apa yang dimaksud dengan Inklusi Keuangan?",
        "expected_keywords": ["ketersediaan", "akses", "lembaga", "produk", "jasa keuangan"]
    },
    {
        "question": "Apa tugas utama dari Direksi Bank?",
        "expected_keywords": ["kepengurusan", "operasional", "tanggung jawab"]
    },
    {
        "question": "Apakah aset kripto diawasi oleh OJK atau Bappebti?",
        "expected_keywords": ["bappebti", "komoditi", "ojk", "peralihan"]
    },
    {
        "question": "Berapa batas usia pensiun normal karyawan menurut undang-undang tenaga kerja?",
        "expected_keywords": ["56", "57", "pensiun", "undang-undang"]
    },

    # ── Batch 2: 10 New Questions ──────────────────────────────────────────
    {
        # Capital Markets
        "question": "Apa yang dimaksud dengan Reksa Dana menurut peraturan OJK?",
        "expected_keywords": ["wadah", "portofolio", "efek", "manajer investasi"]
    },
    {
        # Insurance law
        "question": "Apa kewajiban utama perusahaan asuransi dalam membayar klaim nasabah?",
        "expected_keywords": ["klaim", "membayar", "polis", "premi"]
    },
    {
        # UMKM / SME financing
        "question": "Apakah OJK mengatur pembiayaan untuk Usaha Mikro Kecil dan Menengah (UMKM)?",
        "expected_keywords": ["umkm", "mikro", "kecil", "pembiayaan"]
    },
    {
        # Specific labor — severance pay
        "question": "Apa yang dimaksud dengan uang pesangon dalam hubungan kerja?",
        "expected_keywords": ["pesangon", "pemutusan", "hubungan kerja", "pengusaha"]
    },
    {
        # Syariah finance — murabahah
        "question": "Apa itu akad Murabahah dalam perbankan syariah?",
        "expected_keywords": ["jual beli", "harga", "keuntungan", "syariah"]
    },
    {
        # BPJS — JHT claim conditions
        "question": "Dalam kondisi apa saja Jaminan Hari Tua (JHT) BPJS Ketenagakerjaan dapat dicairkan?",
        "expected_keywords": ["pensiun", "cacat", "meninggal", "berhenti", "klaim"]
    },
    {
        # Trick question — should say it doesn't know
        "question": "Berapa harga saham PT Bank Rakyat Indonesia hari ini?",
        "expected_keywords": ["tidak", "informasi", "dokumen"]
    },
    {
        # Leasing / multifinance
        "question": "Apa yang diatur dalam peraturan OJK tentang perusahaan pembiayaan?",
        "expected_keywords": ["pembiayaan", "leasing", "sewa", "cicilan"]
    },
    {
        # GCG — Good Corporate Governance
        "question": "Apa prinsip-prinsip Tata Kelola Perusahaan yang Baik (GCG) menurut OJK?",
        "expected_keywords": ["transparansi", "akuntabilitas", "pertanggungjawaban", "kemandirian"]
    },
    {
        # BPJS — employer obligations
        "question": "Apa sanksi bagi pemberi kerja yang tidak mendaftarkan karyawannya ke BPJS Ketenagakerjaan?",
        "expected_keywords": ["teguran", "denda", "sanksi", "pelayanan publik"]
    },
]


def get_chroma_collection():
    client = chromadb.PersistentClient(path=CHROMA_DB_DIR)
    return client.get_or_create_collection(
        name="ojk_regulations",
        metadata={"hnsw:space": "cosine"}
    )

def retrieve_public_context(query: str, n_results=5) -> str:
    collection = get_chroma_collection()
    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        where={"visibility": "public"}
    )
    
    contexts = []
    if results and results['documents']:
        for i in range(len(results['documents'][0])):
            doc = results['documents'][0][i]
            meta = results['metadatas'][0][i]
            contexts.append(f"SUMBER: {meta.get('jenis')} {meta.get('nomor')}\n{doc}")
            
    context = "\n\n".join(contexts)
    # Truncate context to prevent API payload size issues
    if len(context) > 4000:
        context = context[:4000] + "\n...[TRUNCATED]"
    return context

def query_llm(question: str, context: str) -> str:
    system_prompt = (
        "Anda adalah AI Legal Analyzer. Gunakan HANYA konteks hukum di bawah ini untuk menjawab. "
        "Jika jawabannya tidak ada di dalam konteks, katakan 'Berdasarkan dokumen yang diberikan, tidak ada informasi'.\n\n"
        f"KONTEKS:\n{context}"
    )
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question}
        ],
        "stream": False,
        "temperature": 0.1
    }
    
    headers = {
        "Authorization": f"Bearer {GLM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    print(f"         [DEBUG] Sending payload of size ~{len(str(payload))} chars...")
    
    try:
        response = requests.post(OLLAMA_URL, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        else:
            print(f"         [API ERROR] Status: {response.status_code}, Msg: {response.text}")
            return "Error: HTTP " + str(response.status_code)
    except requests.exceptions.ConnectionError:
        print("         [API ERROR] Connection aborted (Remote end closed connection). Payload might be too large.")
        return "Error: Connection aborted"
    except Exception as e:
        print(f"         [API ERROR] {e}")
        return "Error: " + str(e)

def run_evaluation():
    print(colored("Starting Automated AI Evaluation...", "cyan", attrs=["bold"]))
    print(f"Total test cases: {len(TEST_SUITE)}\n")
    
    passed = 0
    
    for i, test in enumerate(TEST_SUITE, 1):
        question = test["question"]
        expected = test["expected_keywords"]
        
        print(f"[{i}/{len(TEST_SUITE)}] Testing: {question}")
        
        # 1. Retrieve Context
        start_time = time.time()
        context = retrieve_public_context(question)
        
        # 2. Query LLM
        answer = query_llm(question, context)
        latency = time.time() - start_time
        
        # 3. Grade Answer
        answer_lower = answer.lower()
        matched = [kw for kw in expected if kw.lower() in answer_lower]
        score = len(matched) / len(expected) * 100
        
        is_pass = score > 0 # Require at least 1 keyword for a pass in this simple script
        
        print("\n         " + colored("=== AI Answer ===", "magenta"))
        import textwrap
        print(textwrap.indent(answer, "         "))
        print("         " + colored("=================", "magenta") + "\n")
        
        if is_pass:
            passed += 1
            print(colored(f"  [PASS] Score: {score:.0f}% ({latency:.2f}s)", "green"))
        else:
            print(colored(f"  [FAIL] Score: {score:.0f}% ({latency:.2f}s)", "red"))
            print(f"         Expected keywords missing: {set(expected) - set(matched)}\n")
            
    # Summary
    print("\n" + "="*40)
    print(colored(f"EVALUATION COMPLETE: {passed}/{len(TEST_SUITE)} Passed", "yellow", attrs=["bold"]))
    print("="*40)

if __name__ == "__main__":
    try:
        from termcolor import colored
    except ImportError:
        # Fallback if termcolor is not installed
        def colored(text, *args, **kwargs): return text
        
    run_evaluation()
