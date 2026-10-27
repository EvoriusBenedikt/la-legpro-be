"""
run_all_scrapers.py
====================
Master runner that executes all JDIH scrapers in sequence,
then fast-ingests all new .txt files into the PG chunks store.

Migration M4: the scrapers now write to PostgreSQL (public.regulations) and
fast_ingest_txt.py embeds into the unified chunks table -- run this inside
the backend container so psycopg, the embedder and the vendored MiniLM
weights are available:
    docker exec legpro-backend python /app/run_all_scrapers.py
"""
import json
import os, sys, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.join(BASE_DIR, "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)
SCRAPER_DIR = os.path.join(BASE_DIR, "scraper", "jdih")
sys.path.insert(0, SCRAPER_DIR)

SCRAPERS = [
    ("Kominfo",            "kominfo",          "KominfoScraper"),
    ("BPJS Kesehatan",     "bpjs_kesehatan",   "BPJSKesehatanScraper"),
    ("Kemendag",           "kemendag",         "KemendagScraper"),
    ("PPATK",              "ppatk",            "PPATKScraper"),
    ("LPS",                "lps",              "LPSScraper"),
    ("DPR",                "dpr",              "DPRScraper"),
    ("Kemenko Ekon",       "kemenko_ekon",     "KemenkoEkonScraper"),
    ("Mahkamah Agung",     "mahkamah_agung",   "MahkamahAgungScraper"),
    # Previously built
    ("Bank Indonesia",     "bi",               "BIScraper"),
    ("Bappebti",           "bappebti",         "BappebtiScraper"),
    ("Setkab",             "setkab",           "SetkabScraper"),
    ("BPJS Ketenagakerjaan", "bpjs_ketenagakerjaan", "BPJSKetenagakerjaanScraper"),
]

def _selected(scrapers, inactive):
    return [scraper for scraper in scrapers if scraper[1] not in inactive]


def _inactive_ids():
    from services import pg_service

    return {
        row["scraper_id"]
        for row in pg_service.query(
            "SELECT scraper_id FROM scraper_config WHERE active = false"
        )
    }


def run_all():
    total_start = time.time()
    results = []
    try:
        inactive = _inactive_ids()
    except Exception as e:
        print(f"[WARN] Could not load scraper configuration; running all: {e}")
        inactive = set()
    selected = _selected(SCRAPERS, inactive)

    print("=" * 60)
    print("  MASTER SCRAPER RUNNER")
    print(f"  Running {len(selected)} of {len(SCRAPERS)} scrapers")
    print("=" * 60)

    for label, module_name, class_name in SCRAPERS:
        if module_name in inactive:
            print(f"\n>>> [{label}]\n  [SKIP] {label} (inactive)")
            results.append((module_name, label, "SKIPPED", 0))
            continue
        print(f"\n>>> [{label}]")
        t0 = time.time()
        try:
            mod = __import__(module_name)
            cls = getattr(mod, class_name)
            instance = cls()
            instance.scrape(limit=100)
            elapsed = time.time() - t0
            results.append((module_name, label, "OK", elapsed))
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [ERROR] {label}: {e}")
            results.append((module_name, label, f"ERROR: {e}", elapsed))

    # Fast-ingest all new .txt files
    print("\n" + "=" * 60)
    print("  FAST INGESTING ALL NEW TEXT REGULATIONS")
    print("=" * 60)
    ingest_path = os.path.join(BASE_DIR, "vector_db", "fast_ingest_txt.py")
    os.system(f'python "{ingest_path}"')

    # Summary
    total_elapsed = time.time() - total_start
    m, s = divmod(int(total_elapsed), 60)
    print("\n" + "=" * 60)
    print(f"  ALL DONE  (total: {m:02d}m {s:02d}s)")
    print("=" * 60)
    for _, label, status, elapsed in results:
        icon = "OK" if status == "OK" else "--" if status == "SKIPPED" else "!!"
        print(f"  [{icon}] {label:30s} {elapsed:.1f}s  {status if status not in ('OK', 'SKIPPED') else ''}")
    return results

if __name__ == "__main__":
    result = run_all()
    print("SCRAPE_RESULT_JSON: " + json.dumps([
        {"id": module_name, "label": label, "status": status, "seconds": round(elapsed, 3)}
        for module_name, label, status, elapsed in result
    ]))
