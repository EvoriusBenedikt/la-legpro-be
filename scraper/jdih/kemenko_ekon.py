"""Kemenko Perekonomian Scraper — Koordinasi ekonomi, KEK, investasi"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class KemenkoEkonomiScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Kemenko_Perekonomian")

    CURATED_CORPUS = [
        {
            "judul": "Peraturan Pemerintah Nomor 5 Tahun 2021 tentang Penyelenggaraan Perizinan Berusaha Berbasis Risiko (OSS-RBA)",
            "nomor": "PP No. 5 Tahun 2021",
            "jenis": "Peraturan Pemerintah", "sektor": "Perizinan Usaha", "status": "Berlaku",
            "detail_url": "https://jdih.ekon.go.id/pp-5-2021",
            "content": (
                "PP ini mengatur sistem perizinan berusaha berbasis risiko melalui sistem OSS (Online Single Submission).\n\n"
                "Klasifikasi risiko usaha: Risiko Rendah mendapat Nomor Induk Berusaha (NIB); "
                "Risiko Menengah Rendah mendapat NIB + Sertifikat Standar; Risiko Menengah Tinggi "
                "mendapat NIB + Sertifikat Standar terverifikasi; Risiko Tinggi mendapat NIB + Izin.\n\n"
                "NIB berlaku sebagai: Tanda Daftar Perusahaan (TDP), Angka Pengenal Impor (API), "
                "dan Hak Akses Kepabeanan."
            ),
        },
        {
            "judul": "Undang-Undang Nomor 25 Tahun 2007 tentang Penanaman Modal",
            "nomor": "UU No. 25 Tahun 2007",
            "jenis": "Undang-Undang", "sektor": "Penanaman Modal", "status": "Berlaku",
            "detail_url": "https://jdih.ekon.go.id/uu-25-2007",
            "content": (
                "Pasal 1 — Penanaman Modal adalah segala bentuk kegiatan menanam modal, baik oleh "
                "penanam modal dalam negeri maupun asing untuk melakukan usaha di wilayah Indonesia.\n\n"
                "Pasal 6 — Pemerintah memberikan perlakuan yang sama kepada semua penanam modal "
                "yang berasal dari negara mana pun yang melakukan kegiatan penanaman modal.\n\n"
                "Pasal 18 — Penanam modal yang melakukan penanaman modal mendapat fasilitas: "
                "pengurangan pajak penghasilan (tax holiday); pembebasan atau keringanan bea masuk; "
                "kemudahan pelayanan perizinan."
            ),
        },
        {
            "judul": "Peraturan Presiden Nomor 10 Tahun 2021 tentang Bidang Usaha Penanaman Modal (Daftar Positif Investasi)",
            "nomor": "Perpres No. 10 Tahun 2021",
            "jenis": "Peraturan Presiden", "sektor": "Investasi", "status": "Berlaku",
            "detail_url": "https://jdih.ekon.go.id/perpres-10-2021",
            "content": (
                "Daftar Positif Investasi (DPI) menggantikan Daftar Negatif Investasi (DNI) sebelumnya.\n\n"
                "Seluruh bidang usaha terbuka untuk penanaman modal, kecuali: bidang usaha yang "
                "dinyatakan tertutup (6 bidang, termasuk industri senjata dan narkotika); bidang usaha "
                "yang hanya dapat dilakukan Pemerintah.\n\n"
                "Bidang usaha dengan persyaratan khusus meliputi: bidang usaha yang diprioritaskan; "
                "bidang usaha dengan persyaratan kepemilikan modal asing; bidang usaha dengan "
                "persyaratan perizinan khusus; bidang usaha khusus untuk UMKM dan Koperasi."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  KEMENKO PEREKONOMIAN SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    KemenkoEkonomiScraper().scrape()
