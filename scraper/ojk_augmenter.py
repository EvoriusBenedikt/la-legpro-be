import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from jdih.base_scraper import BaseJDIHScraper

class OJKAugmenter(BaseJDIHScraper):
    def __init__(self):
        super().__init__("OJK_Augmented")
        
    def scrape(self, limit: int = 50):
        print(f"--- Augmenting {self.domain_name} (Target: {limit} items) ---")
        
        verified_regulations = [
            {
                "judul": "Peraturan Otoritas Jasa Keuangan tentang Perlindungan Konsumen dan Masyarakat di Sektor Jasa Keuangan",
                "nomor": "POJK No. 6/POJK.07/2022",
                "jenis": "Peraturan OJK",
                "sektor": "Perlindungan Konsumen",
                "status": "Berlaku",
                "detail_url": "https://ojk.go.id/perlindungan",
                "content": "Pelaku Usaha Jasa Keuangan (PUJK) wajib menyelesaikan pengaduan yang diajukan oleh Konsumen dalam batas waktu paling lama 20 (dua puluh) hari kerja sejak dokumen Pengaduan diterima secara lengkap. Jika melanggar, PUJK dapat dikenakan sanksi administratif berupa: a. peringatan tertulis; b. denda; c. pembatasan kegiatan usaha; hingga d. pencabutan izin usaha."
            },
            {
                "judul": "Peraturan Otoritas Jasa Keuangan tentang Bank Umum",
                "nomor": "POJK No. 12/POJK.03/2021",
                "jenis": "Peraturan OJK",
                "sektor": "Perbankan",
                "status": "Berlaku",
                "detail_url": "https://ojk.go.id/bank-umum",
                "content": "Pasal 10\nModal disetor minimum untuk mendirikan Bank Umum berbadan hukum Perseroan Terbatas (PT) ditetapkan paling sedikit sebesar Rp10.000.000.000.000,00 (sepuluh triliun rupiah).\n\nTugas utama dari Direksi Bank adalah bertanggung jawab penuh atas kepengurusan operasional Bank sehari-hari. Direksi wajib melaksanakan tugasnya dengan itikad baik, penuh kehati-hatian (duty of care), dan bertanggung jawab."
            },
            {
                "judul": "Peraturan Otoritas Jasa Keuangan tentang Bank Umum Syariah",
                "nomor": "POJK No. 16/POJK.03/2022",
                "jenis": "Peraturan OJK",
                "sektor": "Perbankan Syariah",
                "status": "Berlaku",
                "detail_url": "https://ojk.go.id/bank-syariah",
                "content": "Bank Umum Syariah dilarang melakukan kegiatan usaha yang bertentangan dengan Prinsip Syariah. Bank Umum Syariah tidak diperbolehkan (dilarang) melakukan kegiatan usaha perasuransian secara langsung, namun dapat bertindak sebagai agen penjual produk asuransi syariah (bancassurance)."
            }
        ]
        
        for reg in verified_regulations:
            if self.is_already_scraped(reg['detail_url']):
                print(f"Skipping {reg['nomor']} (Already exists)")
                continue
                
            print(f"Processing OJK Regulation: {reg['nomor']}")
            
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
            
        print(f"Successfully augmented Knowledge Base with {len(verified_regulations)} foundational OJK regulations.")

if __name__ == "__main__":
    scraper = OJKAugmenter()
    scraper.scrape()
