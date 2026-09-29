import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── File logging setup ──────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Redirect all print() calls to the logger so nothing is missed
import builtins as _builtins
_real_print = _builtins.print
def _log_print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    logging.info(msg)
_builtins.print = _log_print

# Setup Paths for Parser (internal_docs.py and some routers import pdf_parser from there)
sys.path.append(os.path.join(BASE_DIR, "parser"))

# ── App imports (after load_dotenv: llm_client/email_service read env at import time) ──
import auth
import history
import internal_docs
import templates
from routers import admin, engineer
from routers import chat, repository, knowledge_graph
from routers import compliance, taxonomy
from services.app_state import API_STATS
from services.db_service import init_main_db
from services.scheduler import start_background_loops

# Setup FastAPI App
app = FastAPI(title="Legal Analyzer API")

# ── In-memory API stats for monitoring (FR-31) ──────────────────────────────
# API_STATS / ACTIVE_TASKS live in services.app_state so the middleware below
# and the routers (engineer, repository) all mutate the SAME dict objects.
# Previously `from main import API_STATS` in engineer.py loaded a second
# instance of main.py under `uvicorn api.main:app`, so the stats endpoint
# read dicts the middleware never wrote to.

@app.middleware("http")
async def track_api_stats(request, call_next):
    route = request.url.path
    if route not in API_STATS:
        API_STATS[route] = {"total": 0, "errors": 0}
    API_STATS[route]["total"] += 1
    response = await call_next(request)
    if response.status_code >= 400:
        API_STATS[route]["errors"] += 1
    return response

# ── Startup: contract-expiration email alerts (APScheduler cron) ────────────
@app.on_event("startup")
def startup_event():
    from services.alert_scheduler import start_scheduler
    start_scheduler()

# ── Startup: system metrics & backup asyncio loops ──────────────────────────
@app.on_event("startup")
async def async_startup_event():
    start_background_loops()

# ── Startup: warm ML singletons in the background (2026-09-29 RAG audit) ────
# The embedder (~10 s) and cross-encoder reranker (~2 s) are lazy singletons;
# without warmup the first chat query after every restart paid both loads.
# A background thread keeps readiness fast; the double-checked locks in
# embed_service.get_embedder / rag_service.get_reranker make a concurrent
# first query wait for the warm load instead of double-loading.
@app.on_event("startup")
def warm_ml_models():
    import threading

    def _warm():
        try:
            from services.embed_service import embed_query
            embed_query("warmup")
            from services.rag_service import get_reranker
            get_reranker().predict([["warmup", "warmup"]])
            print("[Startup] ML models warm (embedder + reranker).")
        except Exception as e:
            print(f"[Startup] ML warmup failed (models will lazy-load on first use): {e}")

    threading.Thread(target=_warm, daemon=True).start()

# ── Shutdown: release the PG connection pool (Migration M3) ─────────────────
@app.on_event("shutdown")
def shutdown_event():
    from services.pg_service import close_pool
    close_pool()

# ── Init DB for metadata ────────────────────────────────────────────────────
init_main_db()

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(history.router, prefix="/api", tags=["history"])
app.include_router(internal_docs.router, prefix="/api", tags=["internal_docs"])
app.include_router(templates.router, prefix="/api", tags=["templates"])

app.include_router(admin.router)
app.include_router(engineer.router)
app.include_router(chat.router)
app.include_router(repository.router)
app.include_router(knowledge_graph.router)
app.include_router(compliance.router)
app.include_router(taxonomy.router, prefix="/api", tags=["taxonomy"])

# Serve local PDFs as static files
PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")
os.makedirs(PDFS_DIR, exist_ok=True)
# NOTE: We do NOT use app.mount(StaticFiles) because Starlette mounts bypass CORS middleware.
# Instead we use a regular route (/api/pdf/{filename}, in routers/repository.py) which correctly gets CORS headers.

# Setup CORS to allow frontend communication.
# Tightened: explicit origins (wildcard "*" is rejected by browsers when
# allow_credentials=True, so credentials never worked cross-origin anyway).
# Override per environment, e.g. ALLOWED_ORIGINS="https://app.example.com,http://localhost:5173"
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://legal-analyzer.lintasarta.dev,http://localhost:5173,http://localhost:3000,http://localhost",
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
