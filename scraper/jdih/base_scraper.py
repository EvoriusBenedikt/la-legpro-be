import os
import sqlite3
import requests
import re
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

class BaseJDIHScraper:
    def __init__(self, domain_name):
        self.domain_name = domain_name
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.db_path = os.path.join(self.base_dir, "data", "legal_metadata.db")
        self.pdf_dir = os.path.join(self.base_dir, "data", "pdfs")
        os.makedirs(self.pdf_dir, exist_ok=True)
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        })

    def get_db_connection(self):
        conn = sqlite3.connect(self.db_path)
        return conn

    def is_already_scraped(self, detail_url: str) -> bool:
        conn = self.get_db_connection()
        c = conn.cursor()
        c.execute('SELECT id FROM regulations WHERE detail_url = ?', (detail_url,))
        res = c.fetchone()
        conn.close()
        return res is not None

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
        c = conn.cursor()
        try:
            c.execute('''
                INSERT INTO regulations (domain, judul, nomor, jenis, sektor, status, detail_url, download_url, local_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (self.domain_name, judul, nomor, jenis, sektor, status, detail_url, download_url, local_path))
            conn.commit()
        except sqlite3.IntegrityError:
            pass # already exists
        finally:
            conn.close()

    def _inject_curated(self, corpus: list) -> int:
        """Write a curated corpus list as .txt files and register them in SQLite. Returns injected count."""
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
