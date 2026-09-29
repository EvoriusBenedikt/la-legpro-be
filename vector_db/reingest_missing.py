"""Re-ingest regulations that have a local file but zero chunks
(Migration M4: was a one-shot Chroma repair with a hardcoded 15-ID list).

The legacy script carried a frozen MISSING_IDS set of ojk_metadata.db rows
that failed an early scan pass; that one-shot job is long done and the IDs
are meaningless against the unified PG store, so M4 generalizes it: with no
arguments it heals EVERY gap audit_kb.py can see (downloaded but not
indexed); explicit ids restrict the run.

Usage (container):
    docker exec legpro-backend python /app/vector_db/reingest_missing.py
    docker exec legpro-backend python /app/vector_db/reingest_missing.py 467 465

Chunk ids/md5 scheme and upsert semantics are shared with ingest.py, so
healing a document that partially ingested overwrites its rows in place.
sparse_legacy stays FALSE (bulk corpus was dense-only in legacy).
"""
import hashlib
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "vector_db"))

import script_env

script_env.bootstrap(needs_parser=True)

from pdf_parser import LegalChunker
from services.rag_service import extract_text_hybrid
from services import pg_service
from services.embed_service import embed_documents
from ingest import UPSERT_CHUNK


def find_gaps(only_ids=None):
    """Downloaded public.regulations rows with no chunks (optionally filtered)."""
    sql = (
        "SELECT r.id, r.domain, r.judul, r.nomor, r.jenis, r.sektor, r.status, "
        "r.local_path FROM regulations r "
        "WHERE r.local_path IS NOT NULL AND r.local_path != '' "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = r.id::text) "
    )
    params = None
    if only_ids:
        sql += "AND r.id = ANY(%s) "
        params = (only_ids,)
    sql += "ORDER BY r.id"
    return pg_service.query(sql, params)


def main():
    only_ids = [int(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else None
    rows = find_gaps(only_ids)
    if not rows:
        print("No gaps found -- every downloaded regulation has chunks.")
        return
    print(f"{len(rows)} regulation(s) missing from the chunk store.\n")

    chunker = LegalChunker()

    ok, fail = 0, 0
    for row in rows:
        reg_id_str = str(row["id"])
        # resolve legacy Windows host paths onto this environment's mount
        local_path = script_env.resolve_data_path(row["local_path"])
        filename = os.path.basename(local_path.replace("\\", "/"))
        print(f"\nProcessing reg_id={reg_id_str} | {row['jenis']} {row['sektor']} "
              f"Nomor {row['nomor']} | {filename}")

        if not os.path.exists(local_path):
            print("  [SKIP] File missing on disk")
            fail += 1
            continue

        try:
            if local_path.endswith(".txt"):
                with open(local_path, "r", encoding="utf-8") as f:
                    text = f.read()
            else:
                # (R1 bugfix: hybrid extraction -- PyMuPDF for digital pages,
                # VLM for scanned/garbled ones. The legacy PaddleOCR chain was
                # dead in every shipped image; see bug_reports.md.)
                text = extract_text_hybrid(local_path)

            if not text.strip():
                print("  [WARN] No text extracted — skipping")
                fail += 1
                continue

            meta = {
                "reg_id": reg_id_str,
                "domain": str(row["domain"]) if row["domain"] else "OJK",
                "filename": filename,
                "judul": str(row["judul"] or ""), "nomor": str(row["nomor"] or ""),
                "jenis": str(row["jenis"] or ""), "sektor": str(row["sektor"] or ""),
                "status": str(row["status"] or ""),
            }
            chunks = chunker.chunk_document(text, meta)
            if not chunks:
                print("  [WARN] No chunks produced")
                fail += 1
                continue

            docs, metas, ids = [], [], []
            for i, ch in enumerate(chunks):
                clean = {k: v for k, v in ch["metadata"].items() if v is not None}
                ids.append(hashlib.md5(f"{reg_id_str}_chunk_{i}".encode()).hexdigest())
                docs.append(ch["text"])
                metas.append(clean)

            embeddings = embed_documents(docs)
            with pg_service.get_conn() as conn:
                for chunk_id, doc, m, emb in zip(ids, docs, metas, embeddings):
                    conn.execute(
                        UPSERT_CHUNK,
                        (chunk_id, m.get("reg_id"), doc, m.get("window_context"),
                         m.get("domain"), m.get("jenis"), m.get("judul"),
                         m.get("nomor"), m.get("sektor"), m.get("status"),
                         m.get("filename"), m.get("doc_category"),
                         # visibility defaults to 'public' -- see the parity note in
                         # ingest.py (2026-09-29 corpus fix); NULL = invisible to all.
                         m.get("visibility") or "public", m.get("user_id"), emb))
            print(f"  [OK] {len(docs)} chunks indexed")
            ok += 1

        except Exception as e:
            print(f"  [ERROR] {e}")
            fail += 1

    total = pg_service.query_one("SELECT COUNT(*) AS n FROM chunks")["n"]
    print(f"\n{'='*40}")
    print(f"Done. Success: {ok}  Failed: {fail}")
    print(f"Total chunks now: {total}")


if __name__ == "__main__":
    main()
