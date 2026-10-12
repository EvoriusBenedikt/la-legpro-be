"""Chat-attachment storage layer (2026-10-12) — Legal Opinion chatbot.

Design: la-legpro-doc/plans/2026-10-12-chat-attachments-design.md.
Files live at ``data/chat_attachments/<attachment_id>/<sanitized-basename>``;
the uuid directory is the traversal guard (house pattern: data/pdfs/). Rows
are owner-scoped (users.id as text); admins are refused at the router
(FR-24 parity). This module NEVER touches the corpus pipeline: no writes to
``chunks``, ``data/pdfs/``, or any ingest worker.

PDF text extraction + chunk embedding (process_pdf, retrieval) and the
startup sweep land in Task 2 of the implementation plan; this module owns
intake, metadata, content lookup, and deletion.
"""
import os
import re
import shutil
import uuid

from services import pg_service

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ATTACH_ROOT = os.path.join(BASE_DIR, "data", "chat_attachments")

# Spec §10 — single source of truth for the tunables (env-free in v1).
WHOLE_TEXT_BUDGET = 8000        # chars; <= this also stores whole text on the row
CURRENT_TURN_WHOLE_CAP = 2      # whole-text docs injected per chat request
IMAGES_PER_REQUEST = 4          # vision parts per chat request
RETRIEVAL_TOP_K = 6             # scoped chunk recall per chat request
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 4             # FE-side contract (composer refuses extra picks)
MAX_PER_CONVERSATION = 20
MAX_PDF_PAGES = 100
STALE_PROCESSING_HOURS = 1

# Metadata shape returned to routers and (minus conversation_id/extracted_chars)
# mirrored by the FE AttachmentMeta type. extracted_text is deliberately NOT
# listed: whole texts ride on chat requests, never on metadata endpoints.
_META_COLS = ("id, conversation_id, kind, filename, mime, bytes, status, "
              "error, storage_mode, extracted_chars")

# Magic-byte signatures (spec §7): the extension and the client-sent mime are
# both untrusted; kind AND canonical mime come from the bytes.
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_PDF = b"%PDF"


def sniff_kind(head: bytes):
    """Return 'image' | 'pdf' | None from the first bytes (pass data[:12];
    WebP carries its 'WEBP' tag at offsets 8-11)."""
    if head.startswith(_PNG) or head.startswith(_JPEG):
        return "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image"
    if head.startswith(_PDF):
        return "pdf"
    return None


def _canonical_mime(head: bytes) -> str:
    """Mime implied by the magic bytes — what /content serves, so a lying
    client content_type can never make us serve bytes under a wrong mime."""
    if head.startswith(_PNG):
        return "image/png"
    if head.startswith(_JPEG):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "application/pdf"


def _sanitize(filename: str) -> str:
    base = os.path.basename(filename or "upload")
    return re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"


def save_upload(conversation_id: str, owner: str, filename: str, mime: str, data: bytes) -> dict:
    """Validate, store file + row, return metadata.

    Raises ValueError('oversize' | 'type' | 'limit') which the router maps to
    413 / 415 / 422. Images go 'ready' immediately; PDFs insert as
    'processing' (Task 2 wires the background extraction that flips them).
    The ``mime`` argument is the client-claimed content_type, kept only for
    logging parity — the stored mime is the canonical one from the bytes.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("oversize")
    head = data[:12]
    kind = sniff_kind(head)
    if kind is None:
        raise ValueError("type")
    row = pg_service.query_one(
        "SELECT COUNT(*) AS n FROM chat_attachments WHERE conversation_id = %s AND owner = %s",
        (conversation_id, owner))
    if row and row["n"] >= MAX_PER_CONVERSATION:
        raise ValueError("limit")

    att_id = str(uuid.uuid4())
    safe = _sanitize(filename)
    directory = os.path.join(ATTACH_ROOT, att_id)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, safe), "wb") as f:
        f.write(data)

    status = "ready" if kind == "image" else "processing"
    return pg_service.execute_returning(
        f"INSERT INTO chat_attachments (id, conversation_id, owner, kind, filename, mime, bytes, status) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_META_COLS}",
        (att_id, conversation_id, owner, kind, safe, _canonical_mime(head), len(data), status),
    )[0]


def list_for_conversation(conversation_id: str, owner: str) -> list:
    return pg_service.query(
        f"SELECT {_META_COLS} FROM chat_attachments "
        "WHERE conversation_id = %s AND owner = %s ORDER BY created_at",
        (conversation_id, owner))


def get_content(attachment_id: str, owner: str):
    """(disk path, mime, filename) for an owned, on-disk attachment, else None
    (missing row, foreign row, and vanished file all collapse to the router's
    404 — the FE renders a ghost chip)."""
    row = pg_service.query_one(
        f"SELECT {_META_COLS} FROM chat_attachments WHERE id = %s AND owner = %s",
        (attachment_id, owner))
    if not row:
        return None
    path = os.path.join(ATTACH_ROOT, row["id"], row["filename"])
    if not os.path.isfile(path):
        return None
    return path, row["mime"], row["filename"]


def delete_one(attachment_id: str, owner: str) -> bool:
    row = pg_service.query_one(
        "SELECT id FROM chat_attachments WHERE id = %s AND owner = %s",
        (attachment_id, owner))
    if not row:
        return False
    pg_service.execute("DELETE FROM chat_attachments WHERE id = %s", (row["id"],))
    shutil.rmtree(os.path.join(ATTACH_ROOT, row["id"]), ignore_errors=True)
    return True


def delete_for_conversation(conversation_id: str, owner: str) -> int:
    rows = pg_service.query(
        "SELECT id FROM chat_attachments WHERE conversation_id = %s AND owner = %s",
        (conversation_id, owner))
    for r in rows:
        shutil.rmtree(os.path.join(ATTACH_ROOT, r["id"]), ignore_errors=True)
    return pg_service.execute(
        "DELETE FROM chat_attachments WHERE conversation_id = %s AND owner = %s",
        (conversation_id, owner))


# ── PDF pipeline (Task 2) ───────────────────────────────────────────────────
# Extraction runs once, in a FastAPI BackgroundTasks job scheduled by the
# router: extract_text_hybrid (PyMuPDF digital text + VLM fallback for scans,
# the house pipeline) → adaptive mode by length vs WHOLE_TEXT_BUDGET. BOTH
# modes chunk+embed (whole mode's chunks are the uniform recall fallback for
# later turns); only 'whole' also carries extracted_text on the row.

def _get_chunker():
    """LegalChunker via the house sys.path idiom (internal_docs.py does the
    same); lazy so module import stays cheap."""
    import sys
    parser_dir = os.path.join(BASE_DIR, "parser")
    if parser_dir not in sys.path:
        sys.path.append(parser_dir)
    from pdf_parser import LegalChunker
    return LegalChunker()


def _store_chunks(attachment_id: str, text: str) -> int:
    """Chunk + embed into chat_attachment_chunks; returns chunk count."""
    from services.embed_service import embed_documents
    chunks = _get_chunker().chunk_document(text)
    texts = [c["text"] for c in chunks if c.get("text", "").strip()]
    if not texts:
        # LegalChunker yielded nothing usable — plain splits so scoped recall
        # is never silently chunkless.
        texts = [text[i:i + 1200] for i in range(0, len(text), 1000)]
    embeddings = embed_documents(texts)
    pg_service.execute("DELETE FROM chat_attachment_chunks WHERE attachment_id = %s",
                       (attachment_id,))
    pg_service.execute_many(
        "INSERT INTO chat_attachment_chunks (attachment_id, seq, text, embedding) "
        "VALUES (%s, %s, %s, %s::vector)",
        [(attachment_id, seq, t, emb) for seq, (t, emb) in enumerate(zip(texts, embeddings))])
    return len(texts)


def process_pdf(attachment_id: str) -> None:
    """Background job: flip a 'processing' pdf row to ready(whole|chunked) or
    failed(+Indonesian error). Never raises — the row IS the status channel."""
    row = pg_service.query_one(
        "SELECT id, filename FROM chat_attachments WHERE id = %s AND kind = 'pdf'",
        (attachment_id,))
    if not row:
        return
    path = os.path.join(ATTACH_ROOT, row["id"], row["filename"])
    try:
        import fitz
        with fitz.open(path) as doc:
            pages = doc.page_count
        if pages > MAX_PDF_PAGES:
            raise ValueError(
                f"Dokumen memiliki {pages} halaman, melebihi batas {MAX_PDF_PAGES} halaman.")
        from services.rag_service import extract_text_hybrid
        text = (extract_text_hybrid(path) or "").strip()
        if not text:
            raise ValueError("Tidak ada teks yang dapat diekstrak dari PDF ini.")
        mode = "whole" if len(text) <= WHOLE_TEXT_BUDGET else "chunked"
        n_chunks = _store_chunks(attachment_id, text)
        pg_service.execute(
            "UPDATE chat_attachments SET status = 'ready', storage_mode = %s, "
            "extracted_text = %s, extracted_chars = %s, error = NULL WHERE id = %s",
            (mode, text if mode == "whole" else None, len(text), attachment_id))
        print(f"[chat-attachments] {attachment_id}: ready ({mode}, {len(text)} chars, {n_chunks} chunks)")
    except Exception as e:
        print(f"[chat-attachments] extraction failed for {attachment_id}: {e}")
        pg_service.execute(
            "UPDATE chat_attachments SET status = 'failed', error = %s WHERE id = %s",
            (str(e)[:300] or "Gagal memproses dokumen.", attachment_id))


def sweep_stale_processing() -> int:
    """Startup sweep: a restart kills in-flight background jobs, so rows stuck
    'processing' past the threshold would spin forever. One UPDATE flips them
    to failed with a re-upload instruction. Returns rowcount."""
    return pg_service.execute(
        "UPDATE chat_attachments SET status = 'failed', "
        "error = 'Pemrosesan terhenti karena server dinyalakan ulang. Silakan unggah ulang dokumen.' "
        "WHERE status = 'processing' AND created_at < now() - make_interval(hours => %s::int)",
        (STALE_PROCESSING_HOURS,))


def get_whole_text(attachment_id: str, owner: str):
    """(extracted_text, filename) for an owned ready whole-mode attachment,
    else None. Consumed by the chat endpoint for current-turn injection."""
    row = pg_service.query_one(
        "SELECT extracted_text, filename FROM chat_attachments "
        "WHERE id = %s AND owner = %s AND status = 'ready' AND storage_mode = 'whole'",
        (attachment_id, owner))
    if not row or not row["extracted_text"]:
        return None
    return row["extracted_text"], row["filename"]


def retrieve_attachment_contexts(attachment_ids: list, query: str, top_k: int = RETRIEVAL_TOP_K) -> list:
    """Scoped top-k recall over chat_attachment_chunks for the given attachment
    ids only (never the corpus). Dense cosine pulls a wider candidate pool,
    the house cross-encoder reranks down to top_k; a reranker fault degrades
    to dense order (house pattern: loud print, no crash)."""
    if not attachment_ids:
        return []
    from services.embed_service import embed_query
    qvec = embed_query(query)
    rows = pg_service.query(
        "SELECT c.attachment_id, c.seq, c.text, a.filename, "
        "1 - (c.embedding <=> %s::vector) AS score "
        "FROM chat_attachment_chunks c "
        "JOIN chat_attachments a ON a.id = c.attachment_id "
        "WHERE c.attachment_id = ANY(%s) "
        "ORDER BY c.embedding <=> %s::vector LIMIT %s",
        (qvec, list(attachment_ids), qvec, max(top_k * 4, 24)))
    if not rows:
        return []
    try:
        from services.rag_service import get_reranker
        scores = get_reranker().predict([[query, r["text"]] for r in rows])
        for r, s in zip(rows, scores):
            r["score"] = float(s)
        rows = sorted(rows, key=lambda r: r.get("score", 0), reverse=True)
    except Exception as e:
        print(f"[chat-attachments] rerank failed, keeping dense order: {e}")
    return [{"attachment_id": r["attachment_id"], "filename": r["filename"],
             "seq": r["seq"], "text": r["text"], "score": r["score"]}
            for r in rows[:top_k]]
