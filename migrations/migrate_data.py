#!/usr/bin/env python3
"""migrate_data.py — LA LegPro legacy-store → PostgreSQL data migration (M2).

Loads everything into the schema created by migrations/001_schema.sql
(+ 002_chunks_fts_flag.sql):

  data/legal_metadata.db -> public.regulations, access_grants, audit_logs,
                            kg_nodes, kg_edges, kg_exclusions,
                            kg_rebuild_history, system_metrics, llm_metrics,
                            document_taxonomy
  data/users.db          -> public.users, chat_sessions, chat_messages,
                            compliance_history, document_templates,
                            active_sessions
  data/ojk_metadata.db   -> scraper.regulations
  data/chroma_db/ 'ojk_regulations' + legal_metadata.db chunks_fts
                           -> public.chunks (single unified table)

Safety properties
  * SQLite sources are opened READ-ONLY (URI mode=ro). The Chroma store is
    opened via chromadb.PersistentClient, which MAY write (WAL/settings) —
    point --chroma-dir at a COPY of a live store (this migration's local runs
    read copies; the app itself is stopped at instance-3 cutover, M5).
  * Re-runnable: every INSERT uses ON CONFLICT DO NOTHING; document_taxonomy
    and document_templates are DELETEd first so live rows win over the 001
    seeds. --reset truncates all targets (RESTART IDENTITY) for a clean reload.
  * Column sets are intersected with the live source schema (PRAGMA
    table_info), so source DBs missing later-added columns (e.g. an old
    snapshot without regulations.klasifikasi) still migrate; missing/extra
    columns are reported as warnings.
  * Timestamps are parsed and validated (TEXT 'YYYY-MM-DD HH:MM:SS' UTC ->
    TIMESTAMPTZ); an unparseable value aborts the run with table/row/column.
  * content_tsv indexes only the first 30,000 chars of text (TSV_TEXT_CAP,
    003_chunks_tsv_cap.sql): PG tsvector has a hard 1 MB limit and the live
    corpus contains multi-MB mega-document chunks (found via dry run). Full
    text is preserved in chunks.text.
  * Identity sequences are setval'd after load (max(id)+1, is_called=false).

Chunks merge policy (measured on live data 2026-09-26: 55,749 Chroma
embeddings; chunks_fts 3,581 rows / 2,015 distinct ids, ALL present in Chroma;
~27% of sampled overlapping ids have differing text because document
re-uploads regenerated FTS rows under the same md5('{nomor}_chunk_{i}') id):
  1. Chroma loads first and is canonical: text = Chroma document, embedding,
     metadata 1:1 -> columns, reg_id -> doc_id (TEXT; metadata holds a mix of
     str and int). sparse_legacy = FALSE.
  2. chunks_fts loads second, ON CONFLICT DO NOTHING (only FTS-only ids —
    which the live data does not currently have — would insert, embedding
    NULL), then EVERY distinct FTS id is flagged sparse_legacy = TRUE so the
    M3 sparse-retrieval rewrite can reproduce today's BM25 corpus exactly
    (today's FTS side searches only these rows).
  3. Where FTS text differs from Chroma text, Chroma wins; match/mismatch
    counts against a 300-id sample are included in the report.

Fast load: the chunks HNSW/GIN/btree indexes are dropped before the Chroma
bulk load and recreated after (mirrors 001_schema.sql DDL) unless
--no-fast-load is given.

Usage (local dev, scratch venv with chromadb==1.5.9 + psycopg):
  python migrations/migrate_data.py --data-dir data --reset
Usage (instance-3, inside the backend container during cutover — M5 runbook):
  python /app/migrations/migrate_data.py --data-dir /app/data
  (DATABASE_URL comes from the environment; --chroma-dir should point at a
   copy taken while the API was stopped.)

Exit code 0 on success, 1 on any error (report still written with --report).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from migration_common import (  # noqa: E402
    MigrationError, connect_pg, connect_sqlite_ro, database_url, ensure_schema_extras,
    parse_date, parse_ts, sqlite_has_table, to_bool, utf8_console, vector_literal,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GROUPS = ["legal", "users", "ojk", "chroma", "fts"]

TS, DATE, BOOL, STR = "ts", "date", "bool", "str"

# (source table, target table, [(column, kind)], primary key, delete_first)
LEGAL_TABLES = [
    ("regulations", "regulations", [
        ("id", None), ("domain", None), ("judul", None), ("nomor", None),
        ("jenis", None), ("sektor", None), ("status", None),
        ("detail_url", None), ("download_url", None), ("local_path", None),
        ("klasifikasi", None)], "id", False),
    ("access_grants", "access_grants", [
        ("id", None), ("doc_id", None), ("granted_by", None), ("granted_to", None),
        ("reason", None), ("expires_at", TS), ("created_at", TS)], "id", False),
    ("audit_logs", "audit_logs", [
        ("id", None), ("timestamp", TS), ("user_id", None), ("action_type", None),
        ("resource_id", None), ("details", None)], "id", False),
    ("kg_nodes", "kg_nodes", [
        ("id", None), ("label", None), ("type", None), ("doc_id", None),
        ("created_at", TS)], "id", False),
    ("kg_edges", "kg_edges", [
        ("id", None), ("source_id", None), ("target_id", None), ("relation", None),
        ("doc_id", None), ("created_at", TS)], "id", False),
    ("kg_exclusions", "kg_exclusions", [
        ("id", None), ("entity_name", None), ("created_at", TS)], "id", False),
    ("kg_rebuild_history", "kg_rebuild_history", [
        ("id", None), ("start_time", TS), ("end_time", TS), ("duration_s", None),
        ("nodes_changed", None), ("edges_changed", None), ("status", None)], "id", False),
    ("system_metrics", "system_metrics", [
        ("id", None), ("timestamp", TS), ("cpu", None), ("ram", None), ("disk", None),
        ("metadata_db_mb", None), ("users_db_mb", None)], "id", False),
    ("llm_metrics", "llm_metrics", [
        ("id", None), ("endpoint", None), ("tokens_used", None), ("latency_ms", None),
        ("cost_estimate", None), ("timestamp", TS)], "id", False),
    ("document_taxonomy", "document_taxonomy", [
        ("id", None), ("name", None), ("parent_id", None), ("is_active", BOOL),
        ("created_at", TS), ("updated_at", TS)], "id", True),
]

USERS_TABLES = [
    ("users", "users", [
        ("id", None), ("username", None), ("password_hash", None), ("email", None),
        ("role", None), ("created_at", TS)], "id", False),
    ("chat_sessions", "chat_sessions", [
        ("id", None), ("user_id", None), ("title", None),
        ("created_at", TS), ("updated_at", TS)], "id", False),
    ("chat_messages", "chat_messages", [
        ("id", None), ("session_id", None), ("role", None), ("content", None),
        ("sources_json", None), ("created_at", TS)], "id", False),
    ("compliance_history", "compliance_history", [
        ("id", None), ("user_id", None), ("filename", None), ("results_json", None),
        ("company_name", None), ("expiration_date", DATE), ("created_at", TS)], "id", False),
    ("document_templates", "document_templates", [
        ("id", None), ("title", None), ("description", None), ("content_template", None),
        ("category", None), ("created_at", TS)], "id", True),
    ("active_sessions", "active_sessions", [
        ("user_id", None), ("username", None), ("role", None),
        ("last_seen", TS), ("ip_address", None)], "user_id", False),
]

OJK_TABLES = [
    ("regulations", "scraper.regulations", [
        ("id", None), ("judul", None), ("nomor", None), ("jenis", None),
        ("sektor", None), ("status", None), ("detail_url", None),
        ("download_url", None), ("local_path", None)], "id", False),
]

# Tables whose identity sequence must be re-based after explicit-id loads.
SEQUENCE_TABLES = [
    "public.regulations", "public.audit_logs", "public.kg_edges",
    "public.kg_exclusions", "public.kg_rebuild_history", "public.system_metrics",
    "public.llm_metrics", "public.document_taxonomy", "public.chat_messages",
    "scraper.regulations",
]

ALL_TARGET_TABLES = [
    "regulations", "access_grants", "audit_logs", "kg_nodes", "kg_edges",
    "kg_exclusions", "kg_rebuild_history", "system_metrics", "llm_metrics",
    "document_taxonomy", "chunks", "users", "chat_sessions", "chat_messages",
    "compliance_history", "document_templates", "active_sessions",
    "scraper.regulations",
]

CHUNK_INDEX_DDLS = [
    "CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (content_tsv)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks (doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_filters ON chunks (doc_category, visibility, user_id)",
]
CHUNK_INDEX_NAMES = [
    "idx_chunks_embedding", "idx_chunks_tsv", "idx_chunks_doc_id", "idx_chunks_filters",
]

INSERT_CHUNKS = (
    "INSERT INTO chunks (id, doc_id, text, window_context, domain, jenis, judul, "
    "nomor, sektor, status, filename, doc_category, visibility, user_id, embedding, "
    "sparse_legacy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s) "
    "ON CONFLICT (id) DO NOTHING"
)
INSERT_FTS_CHUNKS = (
    "INSERT INTO chunks (id, doc_id, text, window_context, sparse_legacy) "
    "VALUES (%s,%s,%s,%s,TRUE) ON CONFLICT (id) DO NOTHING"
)


def pg_count(pg, table):
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM %s" % table)
        return cur.fetchone()["n"]


def convert(value, kind, ctx):
    try:
        if kind == TS:
            return parse_ts(value)
        if kind == DATE:
            return parse_date(value)
        if kind == BOOL:
            return to_bool(value)
        if kind == STR:
            return None if value is None else str(value)
        return value
    except ValueError as e:
        raise MigrationError("%s: %s" % (ctx, e))


def load_table(pg, con, src, dst, spec, pk, delete_first, batch_size, warnings):
    """Copy one SQLite table into PG. Returns a result dict."""
    if not sqlite_has_table(con, src):
        warnings.append("source table missing, skipped: %s" % src)
        return {"skipped_missing_source": src}

    src_cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % src).fetchall()]
    cols, kinds = [], []
    for name, kind in spec:
        if name in src_cols:
            cols.append(name)
            kinds.append(kind)
        else:
            warnings.append("%s: column %r absent in source (PG default applies)" % (src, name))
    extra = sorted(set(src_cols) - {n for n, _ in spec})
    if extra:
        warnings.append("%s: source columns ignored (not in target): %s" % (src, extra))

    total = con.execute('SELECT count(*) FROM "%s"' % src).fetchone()[0]
    select = 'SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in cols), src)
    insert = "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT DO NOTHING" % (
        dst, ", ".join(cols), ", ".join(["%s"] * len(cols)))

    before = pg_count(pg, dst)
    t0 = time.time()
    cur_src = con.execute(select)
    with pg.transaction():
        cur = pg.cursor()
        if delete_first:
            cur.execute("DELETE FROM %s" % dst)
            before = 0
        while True:
            rows = cur_src.fetchmany(batch_size)
            if not rows:
                break
            batch = []
            for r_i, row in enumerate(rows):
                batch.append(tuple(
                    convert(row[c_i], kinds[c_i], "%s row %r col %s" % (src, row[0], cols[c_i]))
                    for c_i in range(len(cols))
                ))
            cur.executemany(insert, batch)
    after = pg_count(pg, dst)
    return {
        "source_rows": total,
        "pg_before": before,
        "pg_after": after,
        "inserted": after - before,
        "skipped_existing": total - (after - before),
        "seconds": round(time.time() - t0, 2),
    }


def load_chroma(pg, chroma_dir, batch_size, fast_load, report, warnings):
    try:
        import chromadb
    except ImportError:
        raise MigrationError(
            "chromadb is required for the chroma group (install chromadb==1.5.9 "
            "or use --skip chroma)")

    client = chromadb.PersistentClient(path=chroma_dir)
    names = [getattr(c, "name", str(c)) for c in client.list_collections()]
    report["collections"] = names

    if "legal_docs" in names:
        legacy_n = client.get_collection("legal_docs").count()
        report["legal_docs_count"] = legacy_n
        if legacy_n > 0:
            raise MigrationError(
                "legacy collection 'legal_docs' has %d embeddings — expected 0 "
                "(verified empty 2026-09-25). Investigate before migrating; "
                "re-run with --skip chroma is NOT a fix." % legacy_n)

    col = client.get_collection("ojk_regulations")
    total = col.count()
    report["chroma_count"] = total
    print("chroma: ojk_regulations = %d embeddings (dim check on first batch)" % total)

    if fast_load:
        with pg.transaction():
            cur = pg.cursor()
            for name in CHUNK_INDEX_NAMES:
                cur.execute("DROP INDEX IF EXISTS %s" % name)
        print("chroma: dropped %d indexes for fast load" % len(CHUNK_INDEX_NAMES))

    before = pg_count(pg, "chunks")
    t0 = time.time()
    scanned = empty_text = null_embedding = bad_dim = 0
    offset = 0
    while offset < total:
        got = col.get(limit=batch_size, offset=offset,
                      include=["embeddings", "documents", "metadatas"])
        ids = got["ids"]
        if not ids:
            break
        rows = []
        for i, cid in enumerate(ids):
            meta = got["metadatas"][i] or {}
            doc = got["documents"][i]
            emb = got["embeddings"][i]
            scanned += 1
            if doc is None or doc == "":
                empty_text += 1
                doc = doc if doc is not None else ""
            if emb is None:
                null_embedding += 1
                lit = None
            else:
                if len(emb) != 384:
                    bad_dim += 1
                    if bad_dim <= 5:
                        warnings.append("chroma id %s: embedding dim %d != 384" % (cid, len(emb)))
                lit = vector_literal(emb)

            def mv(key):
                v = meta.get(key)
                return None if v is None else str(v)

            rows.append((cid, mv("reg_id"), doc, mv("window_context"), mv("domain"),
                         mv("jenis"), mv("judul"), mv("nomor"), mv("sektor"),
                         mv("status"), mv("filename"), mv("doc_category"),
                         mv("visibility"), mv("user_id"), lit, False))
        with pg.transaction():
            pg.cursor().executemany(INSERT_CHUNKS, rows)
        offset += batch_size
        if scanned % 10000 < batch_size:
            print("chroma: %d / %d loaded (%.0fs)" % (scanned, total, time.time() - t0))

    if fast_load:
        t1 = time.time()
        with pg.transaction():
            cur = pg.cursor()
            # HNSW build for ~56k × 384-dim vectors exceeds the 64MB default.
            # Parallel builds allocate dynamic shared memory, which fails on
            # Docker's default 64MB /dev/shm ("could not resize shared memory
            # segment") — build single-process with local work_mem instead.
            cur.execute("SET LOCAL maintenance_work_mem = '256MB'")
            cur.execute("SET LOCAL max_parallel_maintenance_workers = 0")
            for ddl in CHUNK_INDEX_DDLS:
                cur.execute(ddl)
        report["index_rebuild_seconds"] = round(time.time() - t1, 1)
        print("chroma: indexes rebuilt in %.1fs" % report["index_rebuild_seconds"])

    after = pg_count(pg, "chunks")
    report.update({
        "chroma_scanned": scanned,
        "empty_text_rows": empty_text,
        "null_embedding_rows": null_embedding,
        "bad_dim_rows": bad_dim,
        "chunks_pg_before": before,
        "chunks_pg_after": after,
        "chunks_inserted": after - before,
        "chroma_seconds": round(time.time() - t0, 1),
    })
    if scanned != total:
        raise MigrationError("chroma scan mismatch: counted %d, scanned %d" % (total, scanned))


def load_fts(pg, con, batch_size, report, warnings):
    if not sqlite_has_table(con, "chunks_fts"):
        warnings.append("chunks_fts missing in source — sparse_legacy stays all-FALSE")
        report["fts"] = {"skipped_missing_source": True}
        return
    rows = con.execute(
        "SELECT chunk_id, doc_id, text, window_context FROM chunks_fts").fetchall()
    total_rows = len(rows)
    by_id = {}
    for r in rows:
        by_id[str(r["chunk_id"])] = r  # last duplicate wins (for reporting only)
    distinct = len(by_id)
    print("fts: %d rows, %d distinct chunk ids (%d duplicate rows)"
          % (total_rows, distinct, total_rows - distinct))

    before = pg_count(pg, "chunks")
    t0 = time.time()
    for i in range(0, total_rows, batch_size):
        batch = [(str(r["chunk_id"]),
                  None if r["doc_id"] is None else str(r["doc_id"]),
                  r["text"] if r["text"] is not None else "",
                  r["window_context"])
                 for r in rows[i:i + batch_size]]
        with pg.transaction():
            pg.cursor().executemany(INSERT_FTS_CHUNKS, batch)
    after = pg_count(pg, "chunks")
    new_inserted = after - before

    ids = list(by_id.keys())
    with pg.transaction():
        cur = pg.cursor()
        cur.execute(
            "UPDATE chunks SET sparse_legacy = TRUE "
            "WHERE id = ANY(%s) AND NOT sparse_legacy", (ids,))
        flagged = cur.rowcount
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM chunks WHERE sparse_legacy")
        sparse_total = cur.fetchone()["n"]

    # Text-equivalence sample for overlapping ids (Chroma text won on conflict).
    sample_ids = ids[:300]
    text_match = text_mismatch = 0
    if sample_ids:
        with pg.cursor() as cur:
            cur.execute("SELECT id, text FROM chunks WHERE id = ANY(%s)", (sample_ids,))
            pg_texts = {r["id"]: r["text"] for r in cur.fetchall()}
        for cid in sample_ids:
            src_text = by_id[cid]["text"] or ""
            if cid in pg_texts and pg_texts[cid] == src_text:
                text_match += 1
            else:
                text_mismatch += 1

    report["fts"] = {
        "source_rows": total_rows,
        "duplicate_rows": total_rows - distinct,
        "distinct_ids": distinct,
        "new_inserted": new_inserted,
        "existing_conflicts": distinct - new_inserted,
        "newly_flagged": flagged,
        "sparse_legacy_total": sparse_total,
        "text_sampled": len(sample_ids),
        "text_match": text_match,
        "text_mismatch": text_mismatch,
        "seconds": round(time.time() - t0, 2),
    }
    if sparse_total != distinct:
        warnings.append(
            "sparse_legacy total %d != distinct FTS ids %d (some FTS ids absent "
            "from chunks?)" % (sparse_total, distinct))


def setval_sequences(pg, report):
    seqs = {}
    with pg.transaction():
        cur = pg.cursor()
        for qualified in SEQUENCE_TABLES:
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('%s', 'id'), "
                "(SELECT COALESCE(MAX(id), 0) + 1 FROM %s), false)" % (qualified, qualified))
            cur.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM %s" % qualified)
            seqs[qualified] = cur.fetchone()["mx"] + 1
    report["sequences_nextval"] = seqs


def reset_targets(pg):
    print("reset: truncating %d target tables (RESTART IDENTITY)" % len(ALL_TARGET_TABLES))
    with pg.transaction():
        pg.cursor().execute(
            "TRUNCATE TABLE %s RESTART IDENTITY" % ", ".join(ALL_TARGET_TABLES))


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data"))
    ap.add_argument("--chroma-dir", default=None,
                    help="default: <data-dir>/chroma_db")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--only", default=None, help="comma list of: " + ",".join(GROUPS))
    ap.add_argument("--skip", default=None, help="comma list of: " + ",".join(GROUPS))
    ap.add_argument("--reset", action="store_true",
                    help="truncate all target tables before loading")
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--no-fast-load", action="store_true",
                    help="keep chunks indexes during the Chroma load (slower)")
    ap.add_argument("--report", default=None, help="write JSON report to this path")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else set(GROUPS)
    skip = set(args.skip.split(",")) if args.skip else set()
    bad = (only | skip) - set(GROUPS)
    if bad:
        ap.error("unknown group(s): %s" % sorted(bad))
    active = [g for g in GROUPS if g in only and g not in skip]
    chroma_dir = args.chroma_dir or os.path.join(args.data_dir, "chroma_db")

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"data_dir": args.data_dir, "chroma_dir": chroma_dir,
                   "groups": active, "reset": args.reset,
                   "batch_size": args.batch_size,
                   "fast_load": not args.no_fast_load},
        "tables": {}, "warnings": [], "errors": [],
    }
    warnings = report["warnings"]
    pg = connect_pg(database_url(args.database_url))
    exit_code = 0
    try:
        ensure_schema_extras(pg)
        if args.reset:
            reset_targets(pg)

        t_start = time.time()
        for group in active:
            print("== group: %s ==" % group)
            if group in ("legal", "users", "ojk"):
                db_name = {"legal": "legal_metadata.db", "users": "users.db",
                           "ojk": "ojk_metadata.db"}[group]
                specs = {"legal": LEGAL_TABLES, "users": USERS_TABLES,
                         "ojk": OJK_TABLES}[group]
                con = connect_sqlite_ro(os.path.join(args.data_dir, db_name))
                try:
                    for src, dst, spec, pk, delete_first in specs:
                        res = load_table(pg, con, src, dst, spec, pk,
                                         delete_first, args.batch_size, warnings)
                        report["tables"][dst] = res
                        if "skipped_missing_source" not in res:
                            print("  %-24s src=%-6d ins=%-6d skip=%-6d (%.1fs)"
                                  % (dst, res["source_rows"], res["inserted"],
                                     res["skipped_existing"], res["seconds"]))
                finally:
                    con.close()
            elif group == "chroma":
                load_chroma(pg, chroma_dir, args.batch_size,
                            not args.no_fast_load, report, warnings)
            elif group == "fts":
                con = connect_sqlite_ro(
                    os.path.join(args.data_dir, "legal_metadata.db"))
                try:
                    load_fts(pg, con, args.batch_size, report, warnings)
                finally:
                    con.close()

        setval_sequences(pg, report)
        report["duration_s"] = round(time.time() - t_start, 1)
        print("done in %.1fs" % report["duration_s"])
    except Exception as e:  # noqa: BLE001 — record, write partial report, exit 1
        exit_code = 1
        report["errors"].append("%s: %s" % (type(e).__name__, e))
        print("ERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
    finally:
        if warnings:
            print("\nwarnings (%d):" % len(warnings))
            for w in warnings:
                print("  - %s" % w)
        if args.report:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False, default=str)
            print("report written: %s" % args.report)
        pg.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
