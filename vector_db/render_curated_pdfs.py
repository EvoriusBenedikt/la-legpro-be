"""Render the curated .txt regulations as viewable PDFs under data/pdfs.

Companion to redownload_missing.py: the 41 curated rows registered by the
scraper _inject_curated() family carry download_url == detail_url (the HTML
page, not a file) and a local_path ending in .txt. Their text lives in-repo
(scraper corpus lists) and in the chunks table -- but 40 of the 41 .txt files
were deleted from disk after ingest, so serve_pdf 404s and the drawer shows
"tidak tersedia di server" even though the document is fully searchable.
This tool renders each curated text into <basename>.pdf at the top level of
data/pdfs, where serve_pdf's basename walk resolves it.

Content source per row (first hit wins, reported per row):
  1. repo   -- the scraper corpus lists, collected by AST-walking every
               scraper/**/*.py for dict literals carrying 'detail_url' +
               'content'. Captures class-level CURATED_CORPUS and method-local
               lists (ojk_augmenter) alike; parses without importing, so no
               module side effects.
  2. disk   -- the .txt itself, when still present (PP_No__5_Tahun_2021.txt).
  3. chunks -- the row's indexed chunks concatenated in seq order (flagged
               CHUNK-RECON: window overlap may duplicate a little text).

Fidelity gate before landing: the rendered PDF must reopen via fitz, hold
>= 1 page, and its extracted text must cover >= 90% of the source characters
(whitespace-normalized), so a layout or encoding disaster never lands.

Modes:
  * no args            dry-run report: per-row source + target, writes nothing
  * --ids 752 758      render exactly these regulation ids
  * --limit N          render the first N candidate rows ordered by id

Rows sharing a basename (duplicate registrations) render once. No DB writes
-- apply migrations/008_curated_txt_to_pdf.sql AFTER rendering to flip
regulations.local_path and chunks.filename from .txt to .pdf together (the
compliance.py sibling join keys on filename; both sides must move as one).

Usage (container):
    docker exec legpro-backend python /app/vector_db/render_curated_pdfs.py
    docker exec legpro-backend python /app/vector_db/render_curated_pdfs.py --limit 999
"""
import ast
import glob as _glob
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import script_env

script_env.bootstrap(needs_api=True)

from services import pg_service

PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")
SCRAPER_DIRS = [
    os.path.join(BASE_DIR, "scraper"),
    os.path.join(BASE_DIR, "scraper", "jdih"),
]

CURATED_SQL = (
    "SELECT r.id, r.domain, r.judul, r.nomor, r.local_path, r.detail_url "
    "FROM regulations r "
    "WHERE r.download_url IS NOT NULL AND r.download_url != '' "
    "AND r.download_url = r.detail_url "
    "ORDER BY r.id"
)

CHUNKS_SQL = "SELECT text FROM chunks WHERE doc_id = %s ORDER BY seq"

# A4 layout in points.
PAGE_W, PAGE_H = 595.28, 841.89
MARGIN = 56.0
BODY_SIZE = 10.5
BODY_LEAD = 14.5

# Latin-1-safe folding: the base-14 Helvetica fonts cover latin-1 only, and
# the curated texts carry typographic quotes/dashes from their sources.
_UNICODE_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2026": "...",
    "\u00a0": " ", "\u200b": "", "\u2022": "-",
}


def _basename_any(path: str) -> str:
    """os.path.basename that also strips Windows separators (mirrors
    api/routers/repository.py)."""
    return os.path.basename((path or "").replace("\\", "/"))


def _sanitize(text: str) -> str:
    """CRLF fold + typographic fold + latin-1 guard for the base-14 fonts."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(_UNICODE_FOLD.get(ch, ch) for ch in text)
    return text.encode("latin-1", "replace").decode("latin-1")


def _corpus_index():
    """{detail_url: entry} harvested from scraper sources by AST walk.

    Also returns {module_name: entry_count_or_error} for the dry-run report.
    literal_eval only accepts pure literals, which is exactly what the corpus
    dicts are (implicit string concatenation collapses to one Constant).
    """
    index, scanned = {}, {}
    for d in SCRAPER_DIRS:
        for path in sorted(_glob.glob(os.path.join(d, "*.py"))):
            name = os.path.basename(path)
            try:
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
            except Exception as e:
                scanned[name] = f"parse error: {type(e).__name__}: {e}"
                continue
            found = 0
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                for el in node.elts:
                    if not isinstance(el, ast.Dict):
                        continue
                    try:
                        entry = ast.literal_eval(el)
                    except Exception:
                        continue
                    if (isinstance(entry, dict)
                            and isinstance(entry.get("detail_url"), str)
                            and isinstance(entry.get("content"), str)):
                        index.setdefault(entry["detail_url"], entry)
                        found += 1
            scanned[name] = found
    return index, scanned


def _find_txt(basename: str):
    """Path of a leftover .txt under PDFS_DIR (top level first), else None."""
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


def _resolve_content(row, corpus):
    """(source_label, body_text) -- ('MISSING', None) when nothing found."""
    entry = corpus.get(row["detail_url"])
    if entry:
        return "repo", entry["content"]
    txt = _find_txt(_basename_any(row["local_path"]))
    if txt:
        with open(txt, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        judul = (row["judul"] or "").strip()
        body = raw
        if judul and body.startswith(judul):
            body = body[len(judul):].lstrip("\n")  # _inject_curated's header
        return "disk", body
    rows = pg_service.query(CHUNKS_SQL, (str(row["id"]),))
    texts = [r["text"] for r in rows if r["text"]]
    if texts:
        return "chunks", "\n\n".join(texts)
    return "MISSING", None


def _wrap(text: str, fontname: str, size: float, width: float):
    """Greedy word wrap measured with fitz.get_text_length; paragraphs kept."""
    import fitz
    out = []
    for para in text.split("\n"):
        para = para.rstrip()
        if not para:
            if out and out[-1] != "":
                out.append("")  # collapse blank runs to one spacer line
            continue
        words = para.split()
        line = ""
        for w in words:
            trial = w if not line else line + " " + w
            if fitz.get_text_length(trial, fontname=fontname, fontsize=size) <= width:
                line = trial
                continue
            if line:
                out.append(line)
            while fitz.get_text_length(w, fontname=fontname, fontsize=size) > width:
                lo, hi = 1, len(w)  # hard-split a word longer than the line
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if fitz.get_text_length(w[:mid], fontname=fontname, fontsize=size) <= width:
                        lo = mid
                    else:
                        hi = mid - 1
                out.append(w[:lo])
                w = w[lo:]
            line = w
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out


def _render_pdf(row, body: str) -> bytes:
    """Title block + paginated body + page-number footers; returns PDF bytes."""
    import fitz
    doc = fitz.open()
    width = PAGE_W - 2 * MARGIN
    judul = _sanitize(row["judul"] or "(tanpa judul)")
    nomor = _sanitize(row["nomor"] or "")
    domain = _sanitize(str(row["domain"] or ""))
    stem = os.path.splitext(_basename_any(row["local_path"]))[0]

    title_lines = _wrap(judul, "hebo", 12.5, width)
    meta_line = f"{nomor}  |  {domain}" if nomor else domain
    note = ("Dokumen curated: dirender dari korpus teks repositori "
            "(bukan berkas sumber asli).")
    note_lines = _wrap(note, "heit", 8.5, width)

    TITLE_LEAD, META_LEAD, NOTE_LEAD, RULE_GAP = 16.0, 16.0, 11.0, 10.0
    header_h = (len(title_lines) * TITLE_LEAD + META_LEAD
                + len(note_lines) * NOTE_LEAD + 2 * RULE_GAP)

    body_lines = _wrap(_sanitize(body), "helv", BODY_SIZE, width)
    first_top = MARGIN + header_h + RULE_GAP
    lines_first = max(1, int((PAGE_H - MARGIN - first_top) / BODY_LEAD))
    lines_rest = max(1, int((PAGE_H - 2 * MARGIN) / BODY_LEAD))

    pages, i, first = [], 0, True
    while True:
        n = lines_first if first else lines_rest
        pages.append(body_lines[i:i + n])
        i += n
        first = False
        if i >= len(body_lines):
            break

    total = len(pages)
    for pno, chunk in enumerate(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        if pno == 0:
            y = MARGIN
            for ln in title_lines:
                page.insert_text((MARGIN, y + 12.5), ln, fontname="hebo", fontsize=12.5)
                y += TITLE_LEAD
            page.insert_text((MARGIN, y + 10), meta_line, fontname="helv", fontsize=10)
            y += META_LEAD
            for ln in note_lines:
                page.insert_text((MARGIN, y + 8.5), ln, fontname="heit", fontsize=8.5)
                y += NOTE_LEAD
            y += RULE_GAP
            page.draw_line(fitz.Point(MARGIN, y), fitz.Point(PAGE_W - MARGIN, y),
                           color=(0.55, 0.55, 0.55), width=0.7)
            top = y + RULE_GAP
        else:
            top = MARGIN
        if chunk:
            rect = fitz.Rect(MARGIN, top, PAGE_W - MARGIN, PAGE_H - MARGIN)
            rc = page.insert_textbox(rect, "\n".join(chunk), fontname="helv",
                                     fontsize=BODY_SIZE,
                                     lineheight=BODY_LEAD / BODY_SIZE)
            if rc < 0:
                doc.close()
                raise RuntimeError(f"body overflow on page {pno + 1}")
        stamp = f"Halaman {pno + 1} dari {total}"
        page.insert_text((MARGIN, PAGE_H - MARGIN + 20), stamp,
                         fontname="heit", fontsize=8)
        page.insert_text((PAGE_W - MARGIN - fitz.get_text_length(stem + ".pdf", fontname="heit", fontsize=8),
                          PAGE_H - MARGIN + 20),
                         stem + ".pdf", fontname="heit", fontsize=8)
    out = doc.tobytes()
    doc.close()
    return out


def _fidelity(pdf_bytes: bytes, source: str):
    """(passes?, reason) -- reopen the render and compare text coverage."""
    import fitz
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = doc.page_count
        extracted = "".join(p.get_text() for p in doc)
        doc.close()
    except Exception as e:
        return False, f"unreadable after render ({type(e).__name__})"
    squeeze = lambda s: re.sub(r"\s+", "", s)
    src_n, ext_n = squeeze(source), squeeze(extracted)
    if pages < 1:
        return False, "zero pages"
    ratio = len(ext_n) / len(src_n) if src_n else 0.0
    if ratio < 0.9:
        return False, f"text coverage {ratio:.0%} < 90%"
    return True, f"{pages}p, coverage {ratio:.0%}"


def render_one(row, corpus):
    """(status, detail) with status in ok|skip|fail -- never raises."""
    stem = os.path.splitext(_basename_any(row["local_path"]))[0]
    target = os.path.join(PDFS_DIR, stem + ".pdf")
    if os.path.isfile(target):
        return "skip", "already rendered"
    label, body = _resolve_content(row, corpus)
    if body is None:
        return "fail", "no content source (repo/disk/chunks all empty)"
    body = body.strip()
    if len(body) < 40:
        return "fail", f"{label} body too short ({len(body)} chars)"
    try:
        pdf_bytes = _render_pdf(row, body)
    except Exception as e:
        return "fail", f"render error ({type(e).__name__}: {e})"
    ok, reason = _fidelity(pdf_bytes, body)
    if not ok:
        return "fail", f"fidelity gate: {reason}"
    with open(target, "wb") as f:
        f.write(pdf_bytes)
    return "ok", f"[{label}] {len(pdf_bytes)} bytes, {reason} -> data/pdfs/{stem}.pdf"


def _unique_targets(rows):
    """Deduplicate rows sharing a basename (duplicate registrations)."""
    seen, targets = set(), []
    for r in rows:
        stem = os.path.splitext(_basename_any(r["local_path"]))[0]
        if stem in seen:
            continue
        seen.add(stem)
        targets.append((stem, r))
    return targets


def main():
    args = sys.argv[1:]
    ids = limit = None
    if "--ids" in args:
        ids = [int(a) for a in args[args.index("--ids") + 1:]]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    corpus, scanned = _corpus_index()
    with_entries = {k: v for k, v in scanned.items() if isinstance(v, int) and v}
    print(f"Corpus index: {len(corpus)} entries by detail_url "
          f"from {len(with_entries)} module(s): "
          + ", ".join(f"{k}({v})" for k, v in sorted(with_entries.items()))
          + "\n")

    rows = pg_service.query(CURATED_SQL)
    if ids is not None:
        wanted = set(ids)
        for mid in sorted(wanted - {r["id"] for r in rows}):
            print(f"[SKIP] id={mid} not a curated row")
        rows = [r for r in rows if r["id"] in wanted]
    elif limit is not None:
        rows = rows[:limit]
    targets = _unique_targets(rows)

    if ids is None and limit is None:
        print(f"{len(rows)} curated regulation row(s), {len(targets)} unique document(s).\n")
        per = {}
        for stem, r in targets:
            label, body = _resolve_content(r, corpus)
            per.setdefault(label, []).append((stem, r, body))
        for label in sorted(per):
            print(f"  source {label}: {len(per[label])}")
        print("\nTargets:")
        for label in sorted(per):
            for stem, r, body in per[label]:
                n = len(body) if body else 0
                print(f"  id={r['id']} {r['domain']} -> {stem}.pdf  [{label}, {n} chars]")
        print("\nDry run -- nothing rendered. Use --ids / --limit to render.")
        return

    ok = skip = fail = 0
    rendered = []
    for stem, row in targets:
        status, detail = render_one(row, corpus)
        print(f"id={row['id']} {row['domain']} | {stem}.pdf\n  [{status.upper()}] {detail}")
        if status == "ok":
            ok += 1
            rendered.append(stem + ".pdf")
        elif status == "skip":
            skip += 1
        else:
            fail += 1
    print(f"\n{'=' * 40}")
    print(f"Done. Rendered: {ok}  Skipped: {skip}  Failed: {fail}")
    if rendered:
        print("Next: apply migrations/008_curated_txt_to_pdf.sql (flips local_path")
        print("+ chunks.filename .txt -> .pdf together), then rebuild the delta tar.")


if __name__ == "__main__":
    main()
