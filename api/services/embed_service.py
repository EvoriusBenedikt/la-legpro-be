"""Query/document embedding for the pgvector store (Migration M3 -- cutover).

Every vector migrated in M2 was produced by ChromaDB's default embedding
function (all-MiniLM-L6-v2 on the ONNX runtime). ChromaDB is going away, so
the app embeds texts itself with sentence-transformers -- already a
dependency for the CrossEncoder reranker -- loading the same MiniLM model.
Torch and ONNX outputs differ only in the last bits; ranking parity was
measured during the M3 cutover (see the la-legpro-doc update log).

Lazy singleton like rag_service.get_reranker(): importing this module costs
nothing, the model loads on first use (a cold HF cache downloads ~90 MB once).

Outputs are pgvector text literals ("[v1,v2,...]") for use with ``%s::vector``
casts -- no pgvector Python package needed, same convention as the M2
migration scripts.
"""
import os
import threading

from services.pg_service import vector_literal

# Same model ChromaDB's default embedding function used for every stored
# vector; override only with a 384-dim normalized MiniLM-compatible model.
EMBEDDER_MODEL_NAME = os.environ.get("EMBEDDER_MODEL_NAME", "all-MiniLM-L6-v2")

_embedder = None
_embedder_lock = threading.Lock()


def get_embedder():
    """Return the process-wide SentenceTransformer, loading it on first use.

    Double-checked locking (2026-09-29 RAG audit): the startup warm thread and
    a concurrent first query must not load the model twice.
    """
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from sentence_transformers import SentenceTransformer
                print(f"Initializing SentenceTransformer embedder: {EMBEDDER_MODEL_NAME}")
                _embedder = SentenceTransformer(EMBEDDER_MODEL_NAME)
    return _embedder


def embed_query(text: str) -> str:
    """Embed a single search query; returns a pgvector literal string."""
    emb = get_embedder().encode([text], normalize_embeddings=True)[0]
    return vector_literal(emb)


def embed_documents(texts) -> list:
    """Embed a batch of chunk texts; returns pgvector literal strings."""
    texts = list(texts)
    if not texts:
        return []
    embs = get_embedder().encode(
        texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
    )
    return [vector_literal(e) for e in embs]
