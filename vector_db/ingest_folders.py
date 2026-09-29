"""Register PDFs found in data/pdfs/<subfolder>/ into public.regulations and
vector-ingest the new ones (Migration M4: was SQLite -> ingest.py/ChromaDB).

The legacy `CREATE TABLE IF NOT EXISTS regulations` bootstrap is gone -- the
PG schema is owned by migrations/001_schema.sql (auto-applied on first init).

Run in the container (supported default; the ingest step needs parser+embedder):
    docker exec legpro-backend python /app/vector_db/ingest_folders.py
"""
import os
import sys
import uuid

import psycopg

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import script_env

script_env.bootstrap()

from services import pg_service

PDFS_DIR = os.path.join(BASE_DIR, "data", "pdfs")


def register_local_pdfs():
    # Import after bootstrap so ingest.py's own module-level bootstrap is a
    # no-op duplicate (it re-runs the same idempotent steps).
    from ingest import ingest_documents

    # (Migration M4: the legacy `WHERE local_path = ?` check compared against
    # host-style paths. Migrated rows carry Windows paths while this script
    # computes container paths, so an exact match would re-register every
    # legacy PDF under a new id (duplicate chunks!). Compare basenames across
    # both path styles instead.)
    registered_names = set()
    for r in pg_service.query(
            "SELECT local_path FROM regulations "
            "WHERE local_path IS NOT NULL AND local_path != ''"):
        registered_names.add(r["local_path"].replace("\\", "/").rsplit("/", 1)[-1].lower())

    new_files_count = 0

    # Look for subdirectories in data/pdfs
    with pg_service.get_conn() as conn:
        for item in sorted(os.listdir(PDFS_DIR)):
            item_path = os.path.join(PDFS_DIR, item)
            if os.path.isdir(item_path):
                domain_name = item.capitalize() # e.g. "Kemnaker", "Kemenkeu"
                print(f"Scanning folder for domain: {domain_name}...")

                for file in sorted(os.listdir(item_path)):
                    if file.endswith('.pdf'):
                        pdf_path = os.path.join(item_path, file)

                        # Check if this file is already registered (by name)
                        if file.lower() in registered_names:
                            print(f"  [SKIP] Already in DB: {file}")
                            continue

                        # Prepare mock metadata
                        judul = file.replace('.pdf', '')
                        doc_id = str(uuid.uuid4())[:8]
                        nomor = f"{domain_name}-{doc_id}"
                        jenis = "Peraturan"
                        sektor = domain_name
                        status = "Berlaku"

                        try:
                            # Inner transaction() = SAVEPOINT: a rejected row
                            # (legacy caught sqlite3.IntegrityError and moved
                            # on) must not poison the surrounding transaction.
                            with conn.transaction():
                                conn.execute(
                                    "INSERT INTO regulations (domain, judul, nomor, "
                                    "jenis, sektor, status, detail_url, download_url, "
                                    "local_path) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                                    "%s, %s)",
                                    (domain_name, judul, nomor, jenis, sektor, status,
                                     f"local://{doc_id}", "", pdf_path))
                            new_files_count += 1
                            print(f"  [ADD] Registered: {file}")
                        except psycopg.IntegrityError:
                            print(f"  [ERROR] Database integrity error for {file}")

    print(f"\nRegistered {new_files_count} new PDFs into the database.")

    if new_files_count > 0:
        print("Starting AI Vector Ingestion for new files...")
        ingest_documents(force_reindex=False)
    else:
        print("No new files to ingest.")


if __name__ == "__main__":
    register_local_pdfs()
    print("Done!")
