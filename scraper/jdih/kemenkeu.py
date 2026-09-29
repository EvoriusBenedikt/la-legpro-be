"""Kemenkeu JDIH scraper — unimplemented stub, not registered in runner.

scrape() is a template placeholder that only prints; nothing is fetched.
The class is importable and convention-compliant (README §6.1) but is
intentionally absent from run_all_scrapers.py SCRAPERS. The Kemenkeu PDFs
in data/pdfs/kemenkeu were ingested by earlier tooling, not by this stub.
To activate: implement the TODO steps in scrape() against
https://jdih.kemenkeu.go.id, then add
("Kemenkeu", "kemenkeu", "KemenkeuScraper") to SCRAPERS.
"""
from base_scraper import BaseJDIHScraper

class KemenkeuScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Kemenkeu")
        self.base_url = "https://jdih.kemenkeu.go.id"
        
    def scrape(self, limit: int = 100):
        print(f"--- Scraping {self.domain_name} (Target: {limit} items) ---")
        # TODO: Implement Kemenkeu specific scraping logic here.
        # It typically involves:
        # 1. Fetching the list of regulations from their search API or HTML list.
        # 2. Extracting detail URLs.
        # 3. For each detail URL, finding the PDF link.
        # 4. Calling self.download_pdf() and self.save_to_db().
        print("Kemenkeu scraper template ready.")

if __name__ == "__main__":
    KemenkeuScraper().scrape(limit=5)
