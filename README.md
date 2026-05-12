# LegalAnalyzer — Indonesian Regulatory AI Platform

> An end-to-end Retrieval-Augmented Generation (RAG) system for Indonesian legal compliance analysis, powered by a 53,000+ chunk knowledge base spanning 12 regulatory bodies and a multimodal (VLM) document compliance checker.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [System Architecture](#2-system-architecture)
3. [Directory Structure](#3-directory-structure)
4. [Technology Stack](#4-technology-stack)
5. [Environment Setup](#5-environment-setup)
6. [Knowledge Base — Data Pipeline](#6-knowledge-base--data-pipeline)
   - [Scrapers](#61-scrapers)
   - [PDF Registration](#62-pdf-registration)
   - [Ingestion](#63-ingestion)
   - [Fast Text Ingestion](#64-fast-text-ingestion)
7. [API Reference](#7-api-reference)
   - [Legal Opinion (RAG Chat)](#71-legal-opinion-rag-chat)
   - [Compliance Checker](#72-compliance-checker)
   - [Legal Repository](#73-legal-repository)
   - [Document Maker](#74-document-maker)
   - [Authentication](#75-authentication)
8. [Frontend Application](#8-frontend-application)
9. [Running the Project](#9-running-the-project)
10. [Evaluation & Accuracy Testing](#10-evaluation--accuracy-testing)
11. [Knowledge Base Coverage](#11-knowledge-base-coverage)
12. [Adding New Data Sources](#12-adding-new-data-sources)
13. [Troubleshooting](#13-troubleshooting)
14. [Architecture Decision Log](#14-architecture-decision-log)

---

## 1. Project Overview

LegalAnalyzer is an AI-powered legal intelligence platform purpose-built for Indonesian regulatory compliance. It combines:

- **RAG (Retrieval-Augmented Generation)**: Answers legal questions grounded in actual regulation text, using smart `doc_category` filtering to prevent cross-contamination (e.g., NDA queries only hit NDA laws).
- **Compliance Checker**: A 5-pass hybrid pipeline that extracts metadata, dates, and cross-checks every clause against the categorized regulatory knowledge base.
- **Hybrid VLM Extraction**: Detects font-encoded corruption and scanned pages, routing them through a Vision Language Model (llama-4-maverick) instead of OCR.
- **Contract Monitor**: A dynamic dashboard for tracking all uploaded documents with real-time expiry tracking (Active, Segera Berakhir, Kedaluwarsa).
- **Document Maker**: AI-assisted generation of formal legal opinion letters.
- **Legal Repository**: Browse, search, and manage all indexed regulations, with an integrated "Export to Word (.doc)" feature for drafting.

**Target users**: Legal teams, compliance officers, and fintech companies managing complex document lifecycles (PKS, NDA) under Indonesian jurisdiction.

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    FRONTEND (React/Vite)                     │
│ LegalOpinion │ Compliance Checker │ Repository │ Monitor │
└───────────────────────┬──────────────────────────────────────┘
                        │ HTTP (port 3000 → 8000)
┌───────────────────────▼─────────────────────────────────┐
│              FastAPI Backend  (api/main.py)               │
│  /api/chat  │  /api/check-compliance  │  /api/analyze    │
└──────┬────────────────┬────────────────┬────────────────┘
       │                │                │
       ▼                ▼                ▼
  ChromaDB         GLM / LLM        SQLite DB
  (Vector Store)   (Labahasa API)   (Metadata)
  53,436 chunks    llama-4-maverick  legal_metadata.db
       ▲
       │  embed & upsert
┌──────┴──────────────────────────────────────────────────┐
│                DATA PIPELINE                             │
│  Scrapers → ingest_folders.py → ingest.py / fast_ingest │
└──────────────────────────────────────────────────────────┘
```

### RAG Flow (per query)

```
User Question
    │
    ▼
ChromaDB cosine similarity search (top-5 chunks)
    │
    ▼
Retrieved legal text + metadata (filtered by doc_category)
    │
    ▼
LLM prompt: system role + retrieved context + user question
    │
    ▼
Answer with citations (Pasal, POJK/PP number, sector)
```

### Compliance Checker Flow

```
Upload PDF
    │
    ├── PyMuPDF extract text per page
    │       ├── ≥80 chars AND vowel ratio ≥15% → use text directly
    │       └── scanned OR garbled text detected
    │               └── render page as PNG → llama-4-maverick vision API
    │
    ▼
extract_pasal_items() & Metadata (Pass 0A: Meta, 0B: Sign Date, 0C: Duration, 0D: Explicit End Date)
    │
    ▼ (for each Pasal)
ChromaDB query (cosine distance < 0.6, filtered by doc_category e.g., PKS vs NDA)
    │
    ▼
LLM compliance verification prompt
    │
    ▼
JSON result: {status, penjelasan, rekomendasi, ai_analysis}
    │
    ▼
Save to SQLite (compliance_history) → Viewable in Contract Monitor
```

---

## 3. Directory Structure

```
LegalAnalyzer/
├── .env                        # API credentials (never commit this)
├── .env.example                # Template for credentials
├── run_all_scrapers.py         # Master runner: all scrapers + fast ingest
├── evaluate_accuracy.py        # 20-question golden test suite
├── check_db.py                 # Database inspection utility
├── query_llm.py                # CLI tool for direct RAG queries
│
├── api/                        # FastAPI backend
│   ├── main.py                 # All endpoints, VLM extractor, compliance checker
│   ├── auth.py                 # JWT authentication
│   ├── history.py              # Conversation history persistence
│   ├── internal_docs.py        # Internal document upload/management
│   ├── requirements.txt        # Python dependencies
│   └── server.log              # Runtime log file (auto-created)
│
├── parser/
│   └── pdf_parser.py           # LegalDocumentParser, LegalChunker (PyMuPDF + PaddleOCR)
│
├── scraper/
│   ├── ojk_scraper.py          # Main OJK JDIH scraper (live + curated)
│   ├── ojk_augmenter.py        # OJK corpus augmentation
│   └── jdih/                   # Domain-specific scrapers
│       ├── base_scraper.py     # Base class: DB save, download, _inject_curated()
│       ├── bi.py               # Bank Indonesia
│       ├── bappebti.py         # Bappebti (crypto/komoditi)
│       ├── setkab.py           # Sekretariat Kabinet (PP, Perpres)
│       ├── kemnaker.py         # Kementerian Ketenagakerjaan
│       ├── kemenkeu.py         # Kementerian Keuangan
│       ├── kominfo.py          # Kominfo / Komdigi (ITE, PDP)
│       ├── bpjs_kesehatan.py   # BPJS Kesehatan (JKN, SJSN)
│       ├── bpjs_ketenagakerjaan.py  # BPJS Ketenagakerjaan (JHT, JP)
│       ├── kemendag.py         # Kemendag (perdagangan, e-commerce)
│       ├── ppatk.py            # PPATK (APU-PPT, KYC)
│       ├── lps.py              # LPS (penjaminan simpanan)
│       ├── dpr.py              # DPR (UU Perbankan, PT, OJK, Pasar Modal)
│       ├── kemenko_ekon.py     # Kemenko Perekonomian (OSS, investasi)
│       └── mahkamah_agung.py   # Mahkamah Agung (PERMA, prosedur)
│
├── vector_db/
│   ├── ingest.py               # Main PDF ingestion with progress bar + skip logic
│   ├── ingest_folders.py       # Scans data/pdfs/ and registers new files in SQLite
│   ├── fast_ingest_txt.py      # Fast ingestion for .txt curated corpus files
│   ├── audit_kb.py             # Audit ChromaDB contents
│   └── reingest_missing.py     # Re-ingest any records missing from ChromaDB
│
├── data/
│   ├── legal_metadata.db       # SQLite: all regulation metadata + file paths
│   ├── chroma_db/              # ChromaDB persistent vector store
│   └── pdfs/                   # Downloaded PDFs + curated .txt files
│       ├── kemenkeu/           # Kemenkeu PDFs subfolder
│       └── kemnaker/           # Kemnaker PDFs subfolder
│
└── frontend/                   # React + Vite frontend
    ├── src/
    │   ├── App.tsx             # Root component, tab router
    │   ├── index.css           # Global design system (dark theme)
    │   ├── components/
    │   │   ├── LegalOpinion.tsx      # RAG chat interface
    │   │   ├── LegalRepository.tsx   # Regulation browser + Word exporter
    │   │   ├── DocumentMaker.tsx     # AI compliance checker
    │   │   ├── ContractMonitor.tsx   # Document dashboard & expiry tracking
    │   │   ├── DocumentDrawer.tsx    # PDF viewer drawer
    │   │   ├── Auth.tsx             # Login / register
    │   │   ├── Account.tsx          # User profile
    │   │   ├── ActivityFeed.tsx     # Right sidebar activity panel
    │   │   ├── Sidebar.tsx          # Left icon navigation
    │   │   └── TopBar.tsx           # Search + notifications bar
    │   └── context/
    │       └── AuthContext.tsx      # JWT auth state management
    └── package.json
```

---

## 4. Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | React 18 + Vite + TypeScript | SPA UI |
| **Styling** | Vanilla CSS (dark theme, glassmorphism) | Design system |
| **Backend** | FastAPI + Uvicorn | REST API server |
| **Vector Store** | ChromaDB (persistent, cosine similarity) | Semantic search |
| **Embeddings** | `sentence-transformers` (all-MiniLM-L6-v2) | Text → vector |
| **LLM / VLM** | llama-4-maverick-instruct (via Labahasa API) | Answers + vision |
| **PDF Parsing** | PyMuPDF (fitz) | Digital text extraction |
| **OCR Fallback** | PaddleOCR + EasyOCR | Scanned page fallback |
| **VLM Extraction** | llama-4-maverick vision | Corrupted/scanned pages |
| **Database** | SQLite (`legal_metadata.db`) | Regulation metadata |
| **Auth** | JWT (python-jose) | User sessions |

---

## 5. Environment Setup

### Prerequisites

- Python 3.10+
- Node.js 18+
- ~4 GB disk space for the knowledge base

### 1. Clone and install Python dependencies

```bash
cd LegalAnalyzer/api
pip install fastapi uvicorn pydantic chromadb sentence-transformers \
            requests python-dotenv pymupdf paddlepaddle paddleocr \
            python-jose[cryptography] passlib[bcrypt]
```

### 2. Install frontend dependencies

```bash
cd LegalAnalyzer/frontend
npm install
```

### 3. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```env
GLM_BASE_URL=https://console.labahasa.ai/v1
GLM_API_KEY=your_key_here
GLM_MODEL=llama-4-maverick-instruct
```

> The Labahasa API is OpenAI-compatible. Any OpenAI-compatible endpoint works here.
> `llama-4-maverick-instruct` supports **vision** — this is required for the VLM compliance checker.

### 4. Initialize the database

The SQLite database is auto-created on first run. To verify:

```bash
python check_db.py
```

Expected output:
```
legal_metadata.db: tables = ['regulations', 'sqlite_sequence']
   regulations: XXXX rows
```

---

## 6. Knowledge Base — Data Pipeline

The knowledge base is a two-layer system:
- **SQLite** (`legal_metadata.db`): stores metadata (title, number, type, sector, file path)
- **ChromaDB** (`data/chroma_db/`): stores embedded text chunks for semantic search

### 6.1 Scrapers

All scrapers inherit from `BaseJDIHScraper` (`scraper/jdih/base_scraper.py`).

**Base class provides:**
- `save_to_db()` — writes metadata to SQLite
- `download_pdf()` — downloads and saves PDF to `data/pdfs/`
- `is_already_scraped()` — deduplication by `detail_url`
- `_inject_curated()` — writes curated `.txt` files and registers them in SQLite

**Scraper strategy — two-phase:**
1. **Live web scrape** — attempts to fetch from the JDIH website
2. **Curated corpus fallback** — if live scrape fails or yields 0 results, injects hand-curated regulation text

**To run all scrapers:**
```bash
python run_all_scrapers.py
```

**To run a single scraper:**
```bash
python scraper/jdih/kominfo.py
```

### 6.2 PDF Registration

When you manually add PDFs to `data/pdfs/` (or subfolders), register them:

```bash
python vector_db/ingest_folders.py
```

This scans `data/pdfs/` recursively and registers any new files in SQLite, skipping already-registered ones.

### 6.3 Ingestion

Processes all SQLite-registered files that are not yet in ChromaDB:

```bash
python vector_db/ingest.py
```

**Features:**
- **Skip logic**: Reads existing `reg_id`s from ChromaDB — only processes new files
- **Live progress bar**: Shows `[####---] 45.2% 279/616 ETA 04:32 filename.pdf`
- **PDF routing**: `.txt` files are read directly; PDFs go through PyMuPDF → OCR fallback
- **Force re-index**: `python vector_db/ingest.py force`

### 6.4 Fast Text Ingestion

For curated `.txt` files only — **no OCR, runs in seconds**:

```bash
python vector_db/fast_ingest_txt.py
```

Use this immediately after running scrapers to push new curated text into ChromaDB without waiting for PDF processing.

**Chunk settings** (in `fast_ingest_txt.py`):
```python
CHUNK_SIZE    = 800   # characters per chunk
CHUNK_OVERLAP = 100   # overlap between consecutive chunks
```

---

## 7. API Reference

The API runs at `http://localhost:8000`. All endpoints require a `Bearer` token (except `/api/auth/*`).

### 7.1 Legal Opinion (RAG Chat)

```
POST /api/chat
Content-Type: application/json

{
  "messages": [
    {"role": "user", "content": "Apa itu P2P Lending menurut OJK?"}
  ]
}
```

**Response:**
```json
{
  "answer": "Berdasarkan POJK No. 10/POJK.05/2022...",
  "sources": [
    {
      "id": "reg_123",
      "jenis": "POJK",
      "nomor": "10/POJK.05/2022",
      "sektor": "IKNB",
      "judul": "Layanan Pendanaan Bersama Berbasis Teknologi",
      "snippet": "..."
    }
  ]
}
```

**RAG pipeline internals:**
- Queries ChromaDB with the user's last message (`n_results=5`)
- Filters by cosine distance threshold
- Builds a system prompt with retrieved chunks as context
- Returns answer + source citations

### 7.2 Compliance Checker

```
POST /api/check-compliance
Content-Type: multipart/form-data

file: <PDF upload>
```

**Response:**
```json
{
  "report": [
    {
      "pasal": "Pasal 7",
      "isi_pasal": "...",
      "status": "SESUAI | BERESIKO | FATAL | TIDAK DIATUR",
      "penjelasan": "...",
      "rekomendasi": "...",
      "ai_analysis": "...",
      "supporting_regulations": [
        {"jenis": "POJK", "nomor": "25", "sektor": "IKNB", "teks": "..."}
      ]
    }
  ]
}
```

**Hybrid text extraction logic:**

| Page condition | Action |
|---|---|
| ≥ 80 chars AND vowel ratio ≥ 15% | PyMuPDF direct text (fast) |
| < 80 chars (scanned) | llama-4-maverick vision API |
| Garbled text (consonant cluster ratio > 12%) | llama-4-maverick vision API |

**Status meanings:**
- `SESUAI` — Clause is compliant with retrieved regulation
- `BERESIKO` — Clause has regulatory risk; recommendation provided
- `FATAL` — Serious violation; immediate action needed
- `TIDAK DIATUR` — No matching regulation found in the knowledge base

### 7.3 Legal Repository

```
GET  /api/regulations?domain=OJK&sektor=Perbankan&search=modal
POST /api/analyze          # Summarize a single regulation with AI
GET  /api/pdf/{filename}   # Serve a PDF file
POST /api/upload-pdf       # Upload a new internal document
```

### 7.4 Document Maker

```
POST /api/generate-document
{
  "document_type": "legal_opinion",
  "context": "...",
  "parties": {...}
}
```

### 7.5 Authentication

```
POST /api/auth/register   # {username, email, password}
POST /api/auth/login      # {username, password} → {access_token}
GET  /api/auth/me         # Returns current user info
```

---

## 8. Frontend Application

### Starting the frontend

```bash
cd frontend
npm run dev
# Opens at http://localhost:3000
```

### Tabs / Pages

| Tab | Component | Description |
|-----|-----------|-------------|
| **Contract Monitor** | `ContractMonitor.tsx` | Dashboard tracking total, active, and expiring documents |
| **Legal Opinion** | `LegalOpinion.tsx` | RAG chat — ask legal questions, get cited answers |
| **Legal Repository** | `LegalRepository.tsx` | Browse indexed regulations; generate drafts & Export to Word (.doc) |
| **Compliance Checker** | `DocumentMaker.tsx` | Upload PDFs for 5-pass clause compliance extraction |
| **Account** | `Account.tsx` | Profile, password change |

### Design System

All styles live in `src/index.css`. Key CSS variables:

```css
--bg-dark:       #16122B   /* page background */
--bg-card:       #221E36   /* card/panel background */
--accent-color:  #A855F7   /* purple accent */
--text-primary:  #f8fafc
--text-secondary: #94a3b8
```

### Layout

```
[Sidebar] [TopBar                          ]
          [Main Content (scrollable)       ] [ActivityFeed]
```

The `dashboard-center` div handles all scrolling. The sidebar and activity feed are fixed.

---

## 9. Running the Project

### Full stack startup

**Terminal 1 — API:**
```bash
cd LegalAnalyzer/api
python main.py
# Uvicorn starts on http://localhost:8000
```

**Terminal 2 — Frontend:**
```bash
cd LegalAnalyzer/frontend
npm run dev
# Vite starts on http://localhost:3000
```

### First-time knowledge base build

```bash
# Step 1: Run all scrapers (curated corpus injection)
python run_all_scrapers.py

# Step 2: Register any manually added PDFs
python vector_db/ingest_folders.py

# Step 3: Ingest all PDFs (long — 30-60 min for 600+ files)
python vector_db/ingest.py

# Step 4: Verify
python check_db.py
```

---

## 10. Evaluation & Accuracy Testing

The golden test suite evaluates the RAG pipeline against 20 representative legal questions.

```bash
python evaluate_accuracy.py
```

### Test Suite Structure

Each test case has a `question` and `expected_keywords` — the AI answer must contain at least one keyword to pass.

**Current 20 questions cover:**

| # | Topic | Expected keywords |
|---|-------|-------------------|
| 1 | P2P Lending definition | layanan pendanaan, teknologi informasi |
| 2 | Trick: current Minister of Finance | tidak, informasi, konteks |
| 3 | Sanksi perlindungan konsumen | peringatan, denda, pencabutan |
| 4 | Modal disetor Bank Umum | 3 triliun, 10 triliun |
| 5 | Bank Syariah dan asuransi | tidak, dilarang |
| 6 | Batas waktu pengaduan PUJK | 20 hari, dua puluh hari |
| 7 | Inklusi Keuangan | ketersediaan, akses, jasa keuangan |
| 8 | Tugas Direksi Bank | kepengurusan, tanggung jawab |
| 9 | Aset kripto: OJK atau Bappebti? | bappebti, komoditi, peralihan |
| 10 | Usia pensiun normal | 56, 57, pensiun |
| 11 | Reksa Dana | wadah, portofolio, manajer investasi |
| 12 | Kewajiban asuransi klaim | klaim, polis, premi |
| 13 | OJK dan UMKM | umkm, mikro, pembiayaan |
| 14 | Uang pesangon | pesangon, pemutusan, hubungan kerja |
| 15 | Akad Murabahah | jual beli, keuntungan, syariah |
| 16 | JHT BPJS pencairan | pensiun, cacat, meninggal, klaim |
| 17 | Trick: BRI stock price | tidak, informasi, dokumen |
| 18 | Perusahaan pembiayaan | pembiayaan, leasing, cicilan |
| 19 | Prinsip GCG | transparansi, akuntabilitas |
| 20 | Sanksi pemberi kerja BPJS | teguran, denda, sanksi |

**Scoring**: Each correct keyword match = 1 point. Score reported as `X/20`.

---

## 11. Knowledge Base Coverage

### Current statistics
- **Total chunks in ChromaDB**: 53,485+
- **Total regulations in SQLite**: 1,000+
- **PDF documents indexed**: 616+
- **Curated text regulations**: 39

### Regulatory bodies covered

| Source | Domain Key | Coverage |
|--------|-----------|----------|
| OJK (Otoritas Jasa Keuangan) | `OJK` | Perbankan, IKNB, Pasar Modal (590 PDFs) |
| Kementerian Keuangan | `Kemenkeu` | PMK, PP, Perpres keuangan (250 PDFs) |
| Kementerian Ketenagakerjaan | `Kemnaker` | UU Ketenagakerjaan, Permenaker (83 PDFs) |
| Bank Indonesia | `BI` | PBI — sistem pembayaran, moneter |
| Bappebti | `Bappebti` | Aset kripto, perdagangan komoditi |
| Sekretariat Kabinet | `Setkab` | PP, Perpres lintas kementerian |
| BPJS Ketenagakerjaan | `BPJS_Ketenagakerjaan` | JHT, JP, JKK, JKM |
| BPJS Kesehatan | `BPJS_Kesehatan` | JKN, SJSN, iuran |
| Kominfo / Komdigi | `Kominfo` | UU ITE, UU PDP, sistem elektronik |
| Kemendag | `Kemendag` | UU Perdagangan, UU Perlindungan Konsumen |
| PPATK | `PPATK` | APU-PPT, KYC, pelaporan TPPU |
| LPS | `LPS` | Penjaminan simpanan, premi |
| DPR | `DPR` | UU PT, UU Pasar Modal, UU Perbankan, UU OJK |
| Kemenko Perekonomian | `Kemenko_Perekonomian` | OSS, DPI investasi |
| Mahkamah Agung | `Mahkamah_Agung` | PERMA mediasi, gugatan sederhana |

---

## 12. Adding New Data Sources

### Option A: Add a new JDIH scraper

1. Create `scraper/jdih/mynewsource.py`:

```python
from base_scraper import BaseJDIHScraper

class MyNewScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("MySource")   # Domain key in SQLite

    CURATED_CORPUS = [
        {
            "judul":      "Full title of the regulation",
            "nomor":      "UU No. X Tahun YYYY",
            "jenis":      "Undang-Undang",   # or POJK, PP, Permendag, etc.
            "sektor":     "Sector name",
            "status":     "Berlaku",
            "detail_url": "https://source.url/unique-id",  # used for deduplication
            "content":    "Pasal 1 — ...\n\nPasal 2 — ...",
        },
    ]

    def scrape(self, limit=100):
        self._inject_curated(self.CURATED_CORPUS)
```

2. Register in `run_all_scrapers.py`:

```python
SCRAPERS = [
    ...
    ("My Source", "mynewsource", "MyNewScraper"),
]
```

3. Run:
```bash
python run_all_scrapers.py
# Then immediately ingest the new .txt files:
python vector_db/fast_ingest_txt.py
```

### Option B: Drop PDFs manually

1. Copy PDFs into `data/pdfs/` (or a subfolder like `data/pdfs/myagency/`)
2. Register them: `python vector_db/ingest_folders.py`
3. Ingest: `python vector_db/ingest.py`

### Option C: Bulk curated text via fast_ingest_txt.py directly

Write `.txt` files to `data/pdfs/` and insert rows into `legal_metadata.db` with `local_path` pointing to the file. Then run `fast_ingest_txt.py`.

---

## 13. Troubleshooting

### ChromaDB `Error loading hnsw index`

The vector index is corrupted (usually from a force-terminated process).

```python
import chromadb
client = chromadb.PersistentClient(path="data/chroma_db")
client.delete_collection("ojk_regulations")
# Then re-run ingestion
```

### `no such table: regulations`

`fast_ingest_txt.py` is pointing to the wrong database. Ensure:
```python
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
DB_PATH  = os.path.join(BASE_DIR, "data", "legal_metadata.db")
```

### PaddleOCR Out-of-Memory crash

PaddleOCR is memory-intensive. Mitigation already applied in `ingest.py` (lazy init). For pure text PDFs, always use `fast_ingest_txt.py` instead of `ingest.py`.

### VLM not triggering for garbled text

Check that `is_text_garbled()` in `api/main.py` is detecting the page. Server logs will show:
```
[QC] Garbled text detected: cluster_ratio=0.18, vowel_ratio=0.09
[VLM] Page 1 → font encoding corruption detected — routing to vision model...
```

If not appearing, the server may need a restart to pick up code changes.

### Ingestion runs but chunks stay at 0

Check that the `.txt` or PDF files are correctly registered in SQLite:
```bash
python check_db.py
```
If `local_path` is `None` or points to a non-existent file, re-run `ingest_folders.py`.

### Frontend CORS error

Ensure the API is running and the frontend is calling `http://localhost:8000`. Check `.env` in the frontend for the API base URL.

---

## 14. Architecture Decision Log

| Decision | Rationale |
|----------|-----------|
| **ChromaDB over Pinecone/Weaviate** | Fully local, no external service, free, persistent |
| **Sentence-transformers embeddings** | Local embedding, no API cost per query |
| **SQLite for metadata** | Lightweight, zero-config, file-based, easy to inspect |
| **Two-database design (SQLite + ChromaDB)** | SQLite = structured metadata search; ChromaDB = semantic text search |
| **Hybrid VLM extraction** | PaddleOCR crashes on Windows due to DLL issues; llama-4-maverick vision is more accurate and uses the existing API |
| **Garbled text detection heuristic** | Font-encoded PDFs pass character count check but produce unreadable text; vowel ratio + consonant cluster detection catches this reliably |
| **Curated corpus strategy** | Many Indonesian JDIH sites have DDoS protection or DNS resolution failures. Hand-curating the 20 most critical laws from each source guarantees minimum viable coverage. |
| **fast_ingest_txt.py separate from ingest.py** | PaddleOCR initialization takes ~30s and may crash; bypassing it for plain text files cuts ingestion from minutes to seconds |
| **OpenAI-compatible API (Labahasa)** | Allows switching LLM provider without changing code; llama-4-maverick is multimodal |
| **RAG over fine-tuning** | Knowledge base grows constantly (new regulations); RAG allows adding data without retraining |
