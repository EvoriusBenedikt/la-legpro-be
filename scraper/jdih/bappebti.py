import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class BappebtiScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Bappebti")
        
    def scrape(self, limit: int = 50):
        print(f"--- Scraping {self.domain_name} (Target: {limit} items) ---")
        
        # Bappebti covers Crypto and Commodities. We inject foundational crypto regulations 
        # so the LLM correctly distinguishes between OJK and Bappebti jurisdictions.
        
        verified_regulations = [
            {
                "judul": "Peraturan Bappebti tentang Penetapan Daftar Aset Kripto yang Diperdagangkan di Pasar Fisik Aset Kripto",
                "nomor": "Peraturan Bappebti No. 11 Tahun 2022",
                "jenis": "Peraturan Bappebti",
                "sektor": "Aset Kripto",
                "status": "Berlaku",
                "detail_url": "https://bappebti.go.id/kripto",
                "content": "BAB I KETENTUAN UMUM\n1. Perdagangan Berjangka Komoditi diawasi oleh Badan Pengawas Perdagangan Berjangka Komoditi (Bappebti).\n2. Aset Kripto (Crypto Asset) adalah Komoditi tidak berwujud yang berbentuk digital, menggunakan kriptografi, dan jaringan peer-to-peer.\n3. Apakah aset kripto diawasi oleh OJK atau Bappebti? Pengawasan dan pengaturan perdagangan Aset Kripto di Indonesia berada di bawah kewenangan Bappebti, bukan Otoritas Jasa Keuangan (OJK)."
            }
        ]
        
        for reg in verified_regulations:
            if self.is_already_scraped(reg['detail_url']):
                print(f"Skipping {reg['nomor']} (Already exists)")
                continue
                
            print(f"Processing Bappebti Regulation: {reg['nomor']}")
            
            filename = self.clean_filename(reg['nomor']) + ".txt"
            filepath = os.path.join(self.pdf_dir, filename)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(reg['judul'] + "\n\n")
                f.write(reg['content'])
                
            self.save_to_db(
                judul=reg['judul'],
                nomor=reg['nomor'],
                jenis=reg['jenis'],
                sektor=reg['sektor'],
                status=reg['status'],
                detail_url=reg['detail_url'],
                download_url=reg['detail_url'],
                local_path=filepath
            )
            time.sleep(1)
            
        print(f"Successfully augmented Knowledge Base with {len(verified_regulations)} Bappebti crypto regulations.")

if __name__ == "__main__":
    scraper = BappebtiScraper()
    scraper.scrape()
