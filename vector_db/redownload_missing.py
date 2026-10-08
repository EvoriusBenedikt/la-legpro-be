"""Re-download regulations whose source file is missing from data/pdfs.

Companion to reingest_missing.py: that tool heals "file on disk but zero
chunks"; this one restores the files themselves for rows that still carry a
download_url. The agency scrapers cannot do this -- is_already_scraped()
skips every existing detail_url BEFORE downloading, so a scraper rerun only
ever adds new regulations and never repairs the legacy rows whose files were
deleted from disk after ingest.

Selection: public.regulations rows whose download_url is non-empty and whose
local_path basename cannot be found anywhere under data/pdfs (the same
basename walk api/routers/repository.py:_find_pdf_file uses when serving
/api/repository/pdf/<filename>, subfolders included).

Modes:
  * no args            dry-run report: per-domain candidate counts + first 15
                       candidates; writes nothing (default on purpose)
  * --ids 2 8 471      download exactly these regulation ids
  * --limit N          download the first N candidates ordered by id

Downloads mirror scraper/jdih/base_scraper.py: browser User-Agent,
verify=False (several JDIH hosts carry broken cert chains), 30 s timeout and
a >5000-byte floor so tiny error pages are never stored as documents, plus
two backoff retries (the pilot hit a transient container DNS failure).

Fidelity gate (user decision 2026-10-07, after the pilot proved
jdih.ojk.go.id now serves 2-page stubs under old DownloadDokumen UUIDs):
every download is inspected in memory before it lands -- PyMuPDF page count
+ text volume vs the duplicate group's legacy chunk volume when a baseline
exists (>= 30%, or >= 10 pages for text-poor probable scans), else absolute
floors (>= 10k text chars or >= 5 pages). Failing files are written to
data/quarantine_full/ (outside the served tree) and listed in the report.

Files land at the row's original data/pdfs-relative location (agency
subfolders kept); existing targets are SKIPped, so reruns are safe. No DB
writes -- run reingest_missing.py with the restored ids afterwards to index
the files (that tool now carries an id-collision guard for duplicate rows).

Usage (container):
    docker exec legpro-backend python /app/vector_db/redownload_missing.py
    docker exec legpro-backend python /app/vector_db/redownload_missing.py --ids 2 8
    docker exec legpro-backend python /app/vector_db/redownload_missing.py --limit 5
"""
import os
import sys
import time

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import script_env

script_env.bootstrap(needs_api=True)

from services import pg_service

PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")
QUARANTINE_DIR = os.path.join(BASE_DIR, "data", "quarantine_full")

CANDIDATE_SQL = (
    "SELECT r.id, r.domain, r.judul, r.nomor, r.local_path, r.download_url "
    "FROM regulations r "
    "WHERE r.download_url IS NOT NULL AND r.download_url != '' "
    # Curated .txt injections (base_scraper._inject_curated) store the HTML
    # detail page as download_url -- downloading it would save a web page as
    # a document. Those 41 rows are the parked "missing .txt" residue; their
    # repair is regenerating the text from the in-repo corpora, not fetching.
    "AND r.download_url IS DISTINCT FROM r.detail_url "
    "ORDER BY r.id"
)

# Legacy chunk text volume across the row's duplicate group. Match key =
# chunks.filename (the local_path basename serve_pdf resolves on) + domain,
# because nomor/judul disagree across duplicate registrations (reg 2 carries
# nomor 9 while its chunk-holding sibling 462 carries nomor 2). 0 when no
# same-domain row has chunks under that filename; the gate then falls back
# to absolute floors.
BASELINE_SQL = (
    "SELECT COALESCE(SUM(LENGTH(c.text)), 0)::int AS n "
    "FROM regulations r "
    "JOIN chunks c ON c.filename = %s "
    "JOIN regulations s ON s.id::text = c.doc_id AND s.domain = r.domain "
    "WHERE r.id = %s"
)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
})


def _basename_any(path: str) -> str:
    """Basename across both path separators (mirrors repository.py)."""
    return os.path.basename((path or "").replace("\\", "/"))


def _find_on_disk(basename: str):
    """Path when serve_pdf's basename walk would resolve it, else None."""
    if not basename:
        return None
    top = os.path.join(PDFS_DIR, basename)
    if os.path.isfile(top):
        return top
    for root, dirs, files in os.walk(PDFS_DIR):
        dirs.sort()
        if basename in files:
            return os.path.join(root, basename)
    return None


def _target_path(local_path: str, basename: str) -> str:
    """Keep the row's data/pdfs-relative location (agency subfolders kept)."""
    norm = (local_path or "").replace("\\", "/")
    idx = norm.rfind("data/pdfs/")
    tail = norm[idx + len("data/pdfs/"):] if idx != -1 else basename
    return os.path.join(PDFS_DIR, *tail.split("/"))


def _fetch(url: str):
    """GET with two backoff retries; returns (response|None, error|None).

    Retries cover connection exceptions AND HTTP 5xx: the full run saw
    jdih.ojk.go.id answer 500 for rows 503-505 while row 506 (same file,
    different UUID) succeeded seconds later -- the 500s are transient load
    shedding, not dead links.
    """
    last = None
    for attempt in range(3):
        try:
            resp = _SESSION.get(url, verify=False, timeout=30)
            if resp.status_code >= 500 and attempt < 2:
                last = f"HTTP {resp.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            return resp, None
        except Exception as e:  # transient DNS/connection failures -- retry
            last = e
            time.sleep(2 * (attempt + 1))
    return None, last


def _sibling_text_baseline(row) -> int:
    """Total legacy chunk text volume across the row's duplicate group (0=none)."""
    r = pg_service.query_one(
        BASELINE_SQL, (_basename_any(row["local_path"]), row["id"]))
    return int(r["n"]) if r and r["n"] else 0


def _fidelity_verdict(content: bytes, baseline: int):
    """(passes_gate?, reason). Runs in memory; nothing is written either way.

    The pilot stub was 2 pages / 4,506 chars against a ~277 KB legacy baseline
    (ratio 0.016), so 0.3 separates stubs from real docs by an order of
    magnitude; text-poor files >= 10 pages pass as probable scans (fitz sees
    no text layer on those, but they are genuine viewable documents).
    """
    try:
        import fitz
        doc = fitz.open(stream=content, filetype="pdf")
        pages = doc.page_count
        text_chars = sum(len(p.get_text()) for p in doc)
        doc.close()
    except Exception as e:
        return False, f"unreadable as PDF ({type(e).__name__})"
    if baseline:
        if text_chars >= 0.3 * baseline:
            return True, f"{pages}p/{text_chars}c vs baseline {baseline}c"
        if pages >= 10:
            return True, f"{pages}p text-poor (probable scan) vs baseline {baseline}c"
        return False, f"stub-like: {pages}p/{text_chars}c vs baseline {baseline}c"
    if text_chars >= 10000 or pages >= 5:
        return True, f"{pages}p/{text_chars}c (no baseline)"
    return False, f"stub-like: {pages}p/{text_chars}c (no baseline)"


def candidates():
    """Rows with a download_url whose file serve_pdf could not resolve."""
    out = []
    for row in pg_service.query(CANDIDATE_SQL):
        basename = _basename_any(row["local_path"])
        if _find_on_disk(basename):
            continue  # file present -- nothing to restore
        out.append({**row, "basename": basename})
    return out


def download_one(row, baseline: int) -> str:
    """'ok: ...' | 'skip: ...' | 'quarantine: ...' | 'fail: ...' -- never raises."""
    target = _target_path(row["local_path"], row["basename"])
    if os.path.isfile(target):
        return "skip: already on disk"
    resp, err = _fetch(row["download_url"])
    if resp is None:
        detail = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
        return f"fail: {detail}"
    if resp.status_code != 200:
        return f"fail: HTTP {resp.status_code}"
    if len(resp.content) <= 5000:
        return "fail: response <=5000 bytes (error page?)"
    passed, reason = _fidelity_verdict(resp.content, baseline)
    if not passed:
        os.makedirs(QUARANTINE_DIR, exist_ok=True)
        qpath = os.path.join(QUARANTINE_DIR, row["basename"])
        with open(qpath, "wb") as f:
            f.write(resp.content)
        return f"quarantine: {reason} -> data/quarantine_full/{row['basename']}"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(resp.content)
    return f"ok: {len(resp.content)} bytes ({reason}) -> {os.path.relpath(target, BASE_DIR)}"


def main():
    args = sys.argv[1:]
    ids = limit = None
    if "--ids" in args:
        ids = [int(a) for a in args[args.index("--ids") + 1:]]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    rows = candidates()
    if ids is not None:
        wanted = set(ids)
        for mid in sorted(wanted - {r["id"] for r in rows}):
            print(f"[SKIP] id={mid} not a candidate (file present or no download_url)")
        rows = [r for r in rows if r["id"] in wanted]
    elif limit is not None:
        rows = rows[:limit]

    if ids is None and limit is None:
        print(f"{len(rows)} regulation(s) missing their file while carrying a download_url.\n")
        per = {}
        for r in rows:
            per.setdefault(str(r["domain"]), []).append(r)
        for domain in sorted(per):
            print(f"  {domain}: {len(per[domain])}")
        print("\nFirst 15 candidates:")
        for r in rows[:15]:
            print(f"  id={r['id']} {r['domain']} Nomor {r['nomor']} -> {r['basename']}")
        print("\nDry run -- nothing downloaded. Use --ids / --limit to restore.")
        return

    ok = fail = skip = quar = 0
    done_ids, quar_ids = [], []
    for row in rows:
        print(f"id={row['id']} {row['domain']} Nomor {row['nomor']} | {row['basename']}")
        result = download_one(row, _sibling_text_baseline(row))
        print(f"  [{result.split(':')[0].upper()}] {result}")
        if result.startswith("skip"):
            skip += 1
        elif result.startswith("ok"):
            ok += 1
            done_ids.append(row["id"])
        elif result.startswith("quarantine"):
            quar += 1
            quar_ids.append(row["id"])
        else:
            fail += 1
    print(f"\n{'='*40}")
    print(f"Done. Downloaded: {ok}  Quarantined: {quar}  Failed: {fail}  Skipped: {skip}")
    if quar_ids:
        print(f"Quarantined ids: {' '.join(str(i) for i in quar_ids)}")
    if done_ids:
        print("Index them with: python /app/vector_db/reingest_missing.py "
              + " ".join(str(i) for i in done_ids))


if __name__ == "__main__":
    main()
