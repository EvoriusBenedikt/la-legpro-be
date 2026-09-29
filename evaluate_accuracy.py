"""Golden-question accuracy evaluation (Migration M4: was ChromaDB + GLM_* env).

Retrieves public chunks for 20 golden questions from the PG chunks table
(embedding via services.embed_service -- the vendored all-MiniLM-L6-v2 that
replaced ChromaDB's default ONNX embedder), answers them through the shared
services.llm_client.call_glm (MODEL_BASE_URL/MODEL_API_KEY/LLAMA_MODEL from
.env, with the production retry + llm_metrics logging) and keyword-grades the
answers.

The legacy script read GLM_BASE_URL/GLM_API_KEY/GLM_MODEL -- env names that
no longer exist in .env -- and imported termcolor, which was never in
requirements.txt; both are fixed by this cutover.

Run in the container (supported default; needs the embedder + LLM reachability):
    docker exec legpro-backend python /app/evaluate_accuracy.py
"""
import os
import sys
import textwrap
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import script_env

script_env.bootstrap()

from services import pg_service
from services.embed_service import embed_query
from services.llm_client import call_glm

# --- Minimal ANSI coloring (replaces the termcolor dependency) ---
def colored(text: str, color: str) -> str:
    codes = {"cyan": "36", "magenta": "35", "green": "32", "red": "31", "yellow": "33"}
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"

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


def retrieve_public_context(query: str, n_results=5) -> str:
    """Dense top-N over public chunks (was a Chroma collection.query with a
    where={"visibility": "public"} metadata filter -- exact-match semantics,
    so rows without visibility never matched there and NULLs don't match here)."""
    q_vec = embed_query(query)
    with pg_service.get_conn() as conn:
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        rows = conn.execute(
            "SELECT text, jenis, nomor FROM chunks "
            "WHERE visibility = 'public' AND embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (q_vec, n_results)).fetchall()

    contexts = [f"SUMBER: {r['jenis']} {r['nomor']}\n{r['text']}" for r in rows]
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

    print(f"         [DEBUG] Sending payload of size ~{len(system_prompt) + len(question)} chars...")

    try:
        return call_glm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            timeout=60,
        )
    except Exception as e:
        # call_glm raises HTTPException with the user-facing Indonesian message;
        # grading below treats any "Error:" answer as a miss, like the legacy.
        print(f"         [API ERROR] {e}")
        return "Error: " + str(e)


def run_evaluation():
    print(colored("Starting Automated AI Evaluation...", "cyan"))
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
    print(colored(f"EVALUATION COMPLETE: {passed}/{len(TEST_SUITE)} Passed", "yellow"))
    print("="*40)

if __name__ == "__main__":
    run_evaluation()
