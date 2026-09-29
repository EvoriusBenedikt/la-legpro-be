import os
import sys

import psycopg
import requests
import re
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# (Migration M4: scraped rows go to the legpro PostgreSQL database --
# public.regulations, the table migrated from legal_metadata.db in M2. The
# shared script_env bootstrap loads .env and builds DATABASE_URL for host
# runs; inside the backend container compose already provides it.)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import script_env
script_env.bootstrap(needs_api=False)

class BaseJDIHScraper:
    def __init__(self, domain_name):
        self.domain_name = domain_name
        self.base_dir = BASE_DIR
        self.pdf_dir = os.path.join(self.base_dir, "data", "pdfs")
        os.makedirs(self.pdf_dir, exist_ok=True)
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        })

    def get_db_connection(self):
        # autocommit mirrors the legacy sqlite3 flow (every statement stood on
        # its own; save_to_db committed per row).
        return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)

    def is_already_scraped(self, detail_url: str) -> bool:
        conn = self.get_db_connection()
        try:
            cur = conn.execute(
                'SELECT id FROM regulations WHERE detail_url = %s', (detail_url,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    def clean_filename(self, text: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_\-]', '_', text)

    def download_pdf(self, download_url: str, filename: str) -> str:
        """Downloads a PDF and returns the local path if successful, else None."""
        filepath = os.path.join(self.pdf_dir, filename)
        try:
            resp = self.session.get(download_url, verify=False, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 5000: # Ensure it's not a tiny error page
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                return filepath
        except Exception as e:
            print(f"Error downloading {download_url}: {e}")
        return None

    def save_to_db(self, judul, nomor, jenis, sektor, status, detail_url, download_url, local_path):
        conn = self.get_db_connection()
        try:
            # ON CONFLICT DO NOTHING reproduces the legacy
            # `except sqlite3.IntegrityError: pass` on the UNIQUE detail_url.
            conn.execute('''
                INSERT INTO regulations (domain, judul, nomor, jenis, sektor, status, detail_url, download_url, local_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (detail_url) DO NOTHING
            ''', (self.domain_name, judul, nomor, jenis, sektor, status, detail_url, download_url, local_path))
        finally:
            conn.close()

    def _inject_curated(self, corpus: list) -> int:
        """Write a curated corpus list as .txt files and register them in the DB. Returns injected count."""
        import time
        injected = 0
        for reg in corpus:
            if self.is_already_scraped(reg["detail_url"]):
                print(f"  [SKIP] {reg['nomor']}")
                continue
            print(f"  [INJ ] {reg['nomor']}")
            filename = self.clean_filename(reg["nomor"]) + ".txt"
            filepath = os.path.join(self.pdf_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"{reg['judul']}\n\n{reg['content']}")
            self.save_to_db(
                judul=reg["judul"], nomor=reg["nomor"],
                jenis=reg["jenis"], sektor=reg["sektor"],
                status=reg["status"], detail_url=reg["detail_url"],
                download_url=reg["detail_url"], local_path=filepath,
            )
            injected += 1
            time.sleep(0.2)
        return injected

    def scrape(self, limit: int = 100):
        """To be implemented by subclasses."""
        raise NotImplementedError()
