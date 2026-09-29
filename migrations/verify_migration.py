#!/usr/bin/env python3
"""verify_migration.py — post-migration reconciliation for LA LegPro M2.

Independently re-reads the legacy stores and checks what migrate_data.py
loaded into PostgreSQL:

  1. Row counts per table (PG < source = FAIL, PG > source = WARN).
  2. Field-level sample diffs per table (random PKs, every shared column,
     normalized: TEXT timestamps -> TIMESTAMPTZ, 1/0 -> bool, float tolerance).
  3. Aggregates (llm_metrics sums, system_metrics timestamp range).
  4. chunks union accounting: PG total == |Chroma ids ∪ chunks_fts ids|,
     embedding-not-null == Chroma count, sparse_legacy == distinct FTS ids.
  5. chunks field/embedding sample vs Chroma (text, 1:1 metadata columns,
     vector element comparison — assumes every Chroma row has an embedding,
     measured true on both local stores 2026-09-26).
  6. KNN parity smoke: PG HNSW and Chroma top-10 distances vs brute-force
     exact ground truth (tie-heavy corpus makes id-overlap the wrong metric;
     exact nearest distance ~0 doubles as the cosine-metric tripwire).
  7. Sparse sanity: a word from a chunk's own text must match its generated
     content_tsv via plainto_tsquery('simple', ...) — the path M3 will use.
  8. Identity sequences: nextval must not collide with existing max(id).

Table specs are imported from migrate_data.py so the two scripts cannot
drift. Sources are opened read-only; --chroma-dir should point at a COPY of
a live store (PersistentClient may write on open), same rule as migrate.

Sources must be FROZEN while migrate+verify run: the legacy backend
continuously appends/prunes system_metrics (and writes audit_logs /
llm_metrics on activity), so verifying while it is up shows expected drift
FAILs on those tables. Stop the app first — that is exactly the cutover
sequence on instance-3 (M5): stop API -> migrate_data.py -> verify -> PG API.

Usage:
  python migrations/verify_migration.py --data-dir data
  python migrations/verify_migration.py --data-dir data --only chroma --knn 30

Exit code 0 = no FAIL (WARN acceptable), 1 = at least one FAIL.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from migration_common import (  # noqa: E402
    TSV_TEXT_CAP, connect_pg, connect_sqlite_ro, database_url,
    ensure_schema_extras, parse_date, parse_ts, parse_vector_literal, redact,
    sqlite_has_table, to_bool, utf8_console,
)
from migrate_data import (  # noqa: E402
    GROUPS, LEGAL_TABLES, OJK_TABLES, REPO_ROOT, SEQUENCE_TABLES, USERS_TABLES,
)

CHECKS = []

CHUNK_META_COLS = ["window_context", "domain", "jenis", "judul", "nomor",
                   "sektor", "status", "filename", "doc_category",
                   "visibility", "user_id"]


def record(name, status, detail=""):
    CHECKS.append({"name": name, "status": status, "detail": detail})
    print("[%-4s] %s%s" % (status, name, (" — " + detail) if detail else ""))


def vals_equal(sv, dv, kind):
    """Compare a source value against a PG value under column-kind normalization."""
    try:
        if kind == "ts":
            sv = parse_ts(sv)
            if isinstance(dv, datetime) and dv.tzinfo:
                dv = dv.astimezone(timezone.utc)
            return sv == dv
        if kind == "date":
            return parse_date(sv) == dv
        if kind == "bool":
            return to_bool(sv) == dv
    except ValueError:
        return False
    if sv is None or dv is None:
        return sv is None and dv is None
    if (isinstance(sv, (int, float)) and not isinstance(sv, bool)
            and isinstance(dv, (int, float)) and not isinstance(dv, bool)):
        return abs(float(sv) - float(dv)) <= 1e-9 * max(1.0, abs(float(sv)), abs(float(dv)))
    return sv == dv


def check_table(pg, con, src, dst, spec, pk, samples):
    if not sqlite_has_table(con, src):
        record("%s count" % dst, "WARN", "source table missing in this snapshot — skipped")
        return
    src_cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % src).fetchall()]
    cols = [(n, k) for n, k in spec if n in src_cols]
    total_src = con.execute('SELECT count(*) FROM "%s"' % src).fetchone()[0]
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM %s" % dst)
        total_pg = cur.fetchone()["n"]
    if total_pg < total_src:
        record("%s count" % dst, "FAIL", "pg=%d < source=%d" % (total_pg, total_src))
    elif total_pg > total_src:
        record("%s count" % dst, "WARN", "pg=%d > source=%d (extra rows)" % (total_pg, total_src))
    else:
        record("%s count" % dst, "PASS", "%d rows" % total_src)

    n = min(samples, total_src)
    if n == 0:
        return
    pks = [r[0] for r in con.execute(
        'SELECT "%s" FROM "%s" ORDER BY RANDOM() LIMIT ?' % (pk, src), (n,)).fetchall()]
    qmarks = ",".join("?" * len(pks))
    src_rows = {row[pk]: row for row in con.execute(
        'SELECT * FROM "%s" WHERE "%s" IN (%s)' % (src, pk, qmarks), pks).fetchall()}
    with pg.cursor() as cur:
        cur.execute("SELECT * FROM %s WHERE %s = ANY(%%s)" % (dst, pk), (pks,))
        pg_rows = {r[pk]: r for r in cur.fetchall()}
    bad = []
    for k in pks:
        s = src_rows.get(k)
        d = pg_rows.get(k)
        if s is None or d is None:
            bad.append((k, "<row>", "missing in PG" if d is None else "phantom in PG"))
            continue
        for name, kind in cols:
            sv, dv = s[name], d.get(name)
            if not vals_equal(sv, dv, kind):
                bad.append((k, name, "%r != %r" % (redact(name, sv), redact(name, dv))))
    if bad:
        record("%s fields" % dst, "FAIL",
               "%d sampled cells differ, e.g. %s" % (len(bad), bad[:3]))
    else:
        record("%s fields" % dst, "PASS", "sample n=%d × %d columns" % (n, len(cols)))


def check_aggregates(pg, con):
    if sqlite_has_table(con, "llm_metrics"):
        s = con.execute("SELECT COUNT(*), COALESCE(SUM(tokens_used),0), "
                        "COALESCE(SUM(latency_ms),0) FROM llm_metrics").fetchone()
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) n, COALESCE(SUM(tokens_used),0) t, "
                        "COALESCE(SUM(latency_ms),0) l FROM llm_metrics")
            d = cur.fetchone()
        ok = (s[0] == d["n"] and float(s[1]) == float(d["t"])
              and float(s[2]) == float(d["l"]))
        record("llm_metrics sums", "PASS" if ok else "FAIL",
               "count/tokens/latency src=(%s,%s,%s) pg=(%s,%s,%s)"
               % (s[0], s[1], s[2], d["n"], d["t"], d["l"]))
    if sqlite_has_table(con, "system_metrics"):
        s = con.execute("SELECT MIN(timestamp), MAX(timestamp) FROM system_metrics").fetchone()
        with pg.cursor() as cur:
            cur.execute("SELECT MIN(timestamp) mn, MAX(timestamp) mx FROM system_metrics")
            d = cur.fetchone()
        ok = (s[0] is None or parse_ts(s[0]) == d["mn"]) and (s[1] is None or parse_ts(s[1]) == d["mx"])
        record("system_metrics range", "PASS" if ok else "FAIL",
               "src=%s..%s pg=%s..%s" % (s[0], s[1], d["mn"], d["mx"]))


def scan_chroma_ids(col, total):
    ids = set()
    include = []
    offset = 0
    while offset < total:
        try:
            got = col.get(limit=10000, offset=offset, include=include)
        except Exception:
            if include == []:
                include = ["metadatas"]  # some builds reject an empty include list
                got = col.get(limit=10000, offset=offset, include=include)
            else:
                raise
        batch = got["ids"]
        if not batch:
            break
        ids.update(batch)
        offset += 10000
    return ids


def check_chunks(pg, data_dir, chroma_dir, knn):
    try:
        import chromadb
    except ImportError:
        record("chunks recon", "FAIL", "chromadb not installed — cannot reconcile")
        return

    fts_ids = set()
    legal = os.path.join(data_dir, "legal_metadata.db")
    if os.path.exists(legal):
        con = connect_sqlite_ro(legal)
        if sqlite_has_table(con, "chunks_fts"):
            fts_ids = {str(r[0]) for r in con.execute("SELECT chunk_id FROM chunks_fts")}
        con.close()

    client = chromadb.PersistentClient(path=chroma_dir)
    col = client.get_collection("ojk_regulations")
    total = col.count()
    chroma_ids = scan_chroma_ids(col, total)
    union = chroma_ids | fts_ids

    with pg.cursor() as cur:
        cur.execute("""SELECT count(*) AS n,
                              count(*) FILTER (WHERE embedding IS NOT NULL) AS emb,
                              count(*) FILTER (WHERE sparse_legacy) AS sparse,
                              count(*) FILTER (WHERE text IS NULL OR text = '') AS empty_text
                       FROM chunks""")
        d = cur.fetchone()

    record("chunks total", "PASS" if d["n"] == len(union) else "FAIL",
           "pg=%d expected |chroma ∪ fts|=%d (chroma=%d, fts distinct=%d)"
           % (d["n"], len(union), len(chroma_ids), len(fts_ids)))
    record("chunks embeddings", "PASS" if d["emb"] == len(chroma_ids) else "FAIL",
           "pg not-null=%d, chroma rows=%d" % (d["emb"], len(chroma_ids)))
    if fts_ids:
        record("chunks sparse_legacy", "PASS" if d["sparse"] == len(fts_ids) else "FAIL",
               "pg=%d, fts distinct ids=%d" % (d["sparse"], len(fts_ids)))
    else:
        record("chunks sparse_legacy", "WARN",
               "no chunks_fts rows in source (pg sparse=%d)" % d["sparse"])
    if d["empty_text"]:
        record("chunks empty text", "WARN", "%d rows with empty text" % d["empty_text"])

    # --- field/embedding sample vs Chroma ---
    n = min(50, len(chroma_ids))
    if n == 0:
        return
    sample = random.sample(sorted(chroma_ids), n)
    got = col.get(ids=sample, include=["embeddings", "documents", "metadatas"])
    by_id = {}
    for i, cid in enumerate(got["ids"]):
        by_id[cid] = (got["documents"][i], got["metadatas"][i] or {}, got["embeddings"][i])
    with pg.cursor() as cur:
        cur.execute("""SELECT id, doc_id, text, window_context, domain, jenis, judul,
                              nomor, sektor, status, filename, doc_category,
                              visibility, user_id, embedding::text AS emb
                       FROM chunks WHERE id = ANY(%s)""", (sample,))
        pg_rows = {r["id"]: r for r in cur.fetchall()}
    bad = []
    emb_max_diff = 0.0
    for cid in sample:
        src = by_id.get(cid)
        row = pg_rows.get(cid)
        if src is None or row is None:
            bad.append((cid[:8], "<row>", "missing" if row is None else "not returned by chroma"))
            continue
        doc, meta, emb = src
        if (doc or "") != (row["text"] or ""):
            bad.append((cid[:8], "text", "len %s vs %s" % (len(doc or ""), len(row["text"] or ""))))
        exp = None if meta.get("reg_id") is None else str(meta.get("reg_id"))
        if exp != row["doc_id"]:
            bad.append((cid[:8], "doc_id", "%r != %r" % (exp, row["doc_id"])))
        for c in CHUNK_META_COLS:
            v = meta.get(c)
            exp = None if v is None else str(v)
            if exp != row[c]:
                bad.append((cid[:8], c, "%r != %r" % (redact(c, exp), redact(c, row[c]))))
        if emb is not None and row["emb"]:
            pv = parse_vector_literal(row["emb"])
            diff = max(abs(float(a) - b) for a, b in zip(emb, pv))
            emb_max_diff = max(emb_max_diff, diff)
    if bad:
        record("chunks field sample", "FAIL", "%d cells differ, e.g. %s" % (len(bad), bad[:3]))
    else:
        record("chunks field sample", "PASS",
               "n=%d rows, all columns identical" % n)
    record("chunks embedding fidelity", "PASS" if emb_max_diff <= 1e-6 else "FAIL",
           "max element diff vs chroma = %.2e (tolerance 1e-6)" % emb_max_diff)

    # --- KNN parity smoke ---
    # ID-overlap (Jaccard) is the wrong metric for this corpus: legal
    # boilerplate creates dense near-duplicate clusters, so both engines
    # legitimately return different members of the same tie cluster. The
    # tie-robust measure is each engine's 10th-result distance vs brute-force
    # exact ground truth (index scans disabled). The exact nearest distance
    # must also be ~0 (the query vector itself is stored) — a metric/copy
    # fault (e.g. cosine vs L2) would show up there.
    if knn > 0 and d["emb"] > 0:
        with pg.cursor() as cur:
            cur.execute("SELECT id, embedding::text AS emb FROM chunks "
                        "WHERE embedding IS NOT NULL ORDER BY random() LIMIT %s", (knn,))
            qrows = cur.fetchall()
        n_results = min(10, total)
        pg_gaps, chroma_gaps, jaccards = [], [], []
        metric_suspect = 0
        for q in qrows:
            emb = parse_vector_literal(q["emb"])
            c_res = col.query(query_embeddings=[emb], n_results=n_results)
            c_ids = list(c_res["ids"][0])
            c_dists = [float(x) for x in c_res["distances"][0]]
            with pg.cursor() as cur:
                # exact ground truth: brute-force seq scan
                cur.execute("SET enable_indexscan = off")
                cur.execute("SET enable_bitmapscan = off")
                cur.execute("SELECT id, embedding <=> %s::vector AS dist FROM chunks "
                            "WHERE embedding IS NOT NULL "
                            "ORDER BY embedding <=> %s::vector LIMIT %s",
                            (q["emb"], q["emb"], n_results))
                exact = cur.fetchall()
                cur.execute("RESET enable_indexscan")
                cur.execute("RESET enable_bitmapscan")
                # PG HNSW side at production-like ef_search
                cur.execute("SET hnsw.ef_search = 100")
                cur.execute("SELECT id, embedding <=> %s::vector AS dist FROM chunks "
                            "WHERE embedding IS NOT NULL "
                            "ORDER BY embedding <=> %s::vector LIMIT %s",
                            (q["emb"], q["emb"], n_results))
                p_rows = cur.fetchall()
                cur.execute("RESET hnsw.ef_search")
            p_ids = [r["id"] for r in p_rows]
            exact_10th = float(exact[-1]["dist"])
            if float(exact[0]["dist"]) > 1e-4:
                metric_suspect += 1
            pg_gaps.append(float(p_rows[-1]["dist"]) - exact_10th)
            chroma_gaps.append(c_dists[-1] - exact_10th)
            uni = len(set(c_ids) | set(p_ids))
            jaccards.append(len(set(c_ids) & set(p_ids)) / uni if uni else 1.0)
        mean_pg_gap = sum(pg_gaps) / len(pg_gaps)
        max_pg_gap = max(pg_gaps)
        mean_ch_gap = sum(chroma_gaps) / len(chroma_gaps)
        mean_j = sum(jaccards) / len(jaccards)
        detail = ("n=%d 10th-dist gap vs exact: pg mean=%.4f max=%.4f, chroma mean=%.4f; "
                  "id Jaccard(pg,chroma) mean=%.2f (informational — tie-heavy corpus); "
                  "metric suspects=%d" % (len(qrows), mean_pg_gap, max_pg_gap,
                                          mean_ch_gap, mean_j, metric_suspect))
        if metric_suspect:
            record("KNN parity", "FAIL", detail)
        elif mean_pg_gap <= 0.01 and max_pg_gap <= 0.05:
            record("KNN parity", "PASS", detail)
        elif mean_pg_gap <= max(0.05, mean_ch_gap + 0.01):
            record("KNN parity", "WARN", detail)
        else:
            record("KNN parity", "FAIL", detail)


def check_sparse(pg):
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM chunks WHERE sparse_legacy")
        n_sparse = cur.fetchone()["n"]
    where = "WHERE sparse_legacy" if n_sparse else ""
    if not n_sparse:
        record("sparse sanity", "WARN", "no sparse_legacy rows — sampling any chunks")
    with pg.cursor() as cur:
        cur.execute("SELECT id, text FROM chunks %s ORDER BY random() LIMIT 20" % where)
        rows = cur.fetchall()
    hits = tested = 0
    misses = []
    for r in rows:
        # Cap mirrors content_tsv's left(text, TSV_TEXT_CAP) — a word beyond
        # the cap is legitimately not indexed.
        words = re.findall(r"[A-Za-z]{6,}", (r["text"] or "")[:TSV_TEXT_CAP])
        if not words:
            continue
        w = max(words, key=len)
        tested += 1
        with pg.cursor() as cur:
            cur.execute("SELECT 1 FROM chunks WHERE id = %s "
                        "AND content_tsv @@ plainto_tsquery('simple', %s)", (r["id"], w))
            if cur.fetchone():
                hits += 1
            else:
                misses.append((r["id"][:8], w))
    if tested == 0:
        record("sparse sanity", "WARN", "no sampleable chunks/words")
    elif hits == tested:
        record("sparse sanity", "PASS",
               "%d/%d sampled chunks matched their own text via content_tsv" % (hits, tested))
    else:
        record("sparse sanity", "FAIL", "%d/%d matched; misses=%s" % (hits, tested, misses[:3]))


def check_sequences(pg):
    bad = []
    with pg.cursor() as cur:
        for t in SEQUENCE_TABLES:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id') AS s", (t,))
            seq = cur.fetchone()["s"]
            cur.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM %s" % t)
            mx = cur.fetchone()["mx"]
            if not seq:
                bad.append((t, "no identity sequence"))
                continue
            if mx == 0:
                continue  # empty table: nextval starts at 1
            cur.execute("SELECT last_value, is_called FROM %s" % seq)
            r = cur.fetchone()
            nxt = r["last_value"] + 1 if r["is_called"] else r["last_value"]
            if nxt is None or nxt <= mx:
                bad.append((t, "nextval=%r <= max(id)=%d" % (nxt, mx)))
    if bad:
        record("identity sequences", "FAIL", str(bad))
    else:
        record("identity sequences", "PASS", "%d tables re-based" % len(SEQUENCE_TABLES))


def main():
    utf8_console()
    ap = argparse.ArgumentParser(
        description="Reconcile PostgreSQL against the legacy stores after migrate_data.py")
    ap.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data"))
    ap.add_argument("--chroma-dir", default=None, help="default: <data-dir>/chroma_db")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--only", default=None, help="comma list of: " + ",".join(GROUPS))
    ap.add_argument("--skip", default=None, help="comma list of: " + ",".join(GROUPS))
    ap.add_argument("--samples", type=int, default=200,
                    help="random rows per table for field-level diffs")
    ap.add_argument("--knn", type=int, default=20,
                    help="KNN parity queries (0 disables)")
    ap.add_argument("--report", default=None, help="write JSON report to this path")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else set(GROUPS)
    skip = set(args.skip.split(",")) if args.skip else set()
    bad = (only | skip) - set(GROUPS)
    if bad:
        ap.error("unknown group(s): %s" % sorted(bad))
    active = [g for g in GROUPS if g in only and g not in skip]
    chroma_dir = args.chroma_dir or os.path.join(args.data_dir, "chroma_db")

    t0 = time.time()
    pg = connect_pg(database_url(args.database_url))
    exit_code = 0
    try:
        ensure_schema_extras(pg)
        for group in active:
            print("== verify group: %s ==" % group)
            if group in ("legal", "users", "ojk"):
                db_name = {"legal": "legal_metadata.db", "users": "users.db",
                           "ojk": "ojk_metadata.db"}[group]
                specs = {"legal": LEGAL_TABLES, "users": USERS_TABLES,
                         "ojk": OJK_TABLES}[group]
                con = connect_sqlite_ro(os.path.join(args.data_dir, db_name))
                try:
                    for src, dst, spec, pk, _delete_first in specs:
                        check_table(pg, con, src, dst, spec, pk, args.samples)
                    if group == "legal":
                        check_aggregates(pg, con)
                finally:
                    con.close()
            elif group == "chroma":
                check_chunks(pg, args.data_dir, chroma_dir, args.knn)
            elif group == "fts":
                pass  # sparse accounting is part of the chunks recon (chroma group)
        if "chroma" in active or "fts" in active:
            check_sparse(pg)
        if {"legal", "users", "ojk"} & set(active):
            check_sequences(pg)
    except Exception as e:  # noqa: BLE001 — record and exit 1
        exit_code = 1
        record("verify run", "FAIL", "%s: %s" % (type(e).__name__, e))
    finally:
        pg.close()

    n_pass = sum(1 for c in CHECKS if c["status"] == "PASS")
    n_warn = sum(1 for c in CHECKS if c["status"] == "WARN")
    n_fail = sum(1 for c in CHECKS if c["status"] == "FAIL")
    duration = round(time.time() - t0, 1)
    print("\n== summary: %d PASS, %d WARN, %d FAIL (%.1fs) =="
          % (n_pass, n_warn, n_fail, duration))
    if n_fail:
        exit_code = 1
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"params": {"data_dir": args.data_dir, "chroma_dir": chroma_dir,
                                  "groups": active, "samples": args.samples,
                                  "knn": args.knn},
                       "checks": CHECKS, "pass": n_pass, "warn": n_warn,
                       "fail": n_fail, "duration_s": duration},
                      f, indent=2, ensure_ascii=False, default=str)
        print("report written: %s" % args.report)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
