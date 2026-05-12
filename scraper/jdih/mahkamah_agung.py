"""Mahkamah Agung JDIH Scraper — PERMA, yurisprudensi, prosedur pengadilan"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class MahkamahAgungScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Mahkamah_Agung")

    CURATED_CORPUS = [
        {
            "judul": "Peraturan Mahkamah Agung Nomor 1 Tahun 2016 tentang Prosedur Mediasi di Pengadilan",
            "nomor": "PERMA No. 1 Tahun 2016",
            "jenis": "Peraturan MA", "sektor": "Prosedur Peradilan", "status": "Berlaku",
            "detail_url": "https://jdih.mahkamahagung.go.id/perma-1-2016",
            "content": (
                "Mediasi adalah cara penyelesaian sengketa melalui proses perundingan untuk memperoleh "
                "kesepakatan Para Pihak dengan dibantu oleh Mediator.\n\n"
                "Pasal 4 — Semua sengketa perdata yang diajukan ke Pengadilan termasuk perkara "
                "perlawanan (verzet) atas putusan verstek wajib terlebih dahulu diupayakan penyelesaian "
                "melalui Mediasi.\n\n"
                "Pasal 24 — Proses Mediasi berlangsung paling lama 30 hari terhitung sejak penetapan "
                "perintah melakukan Mediasi dan dapat diperpanjang 30 hari atas kesepakatan para pihak."
            ),
        },
        {
            "judul": "Peraturan Mahkamah Agung Nomor 2 Tahun 2015 tentang Tata Cara Penyelesaian Gugatan Sederhana",
            "nomor": "PERMA No. 2 Tahun 2015",
            "jenis": "Peraturan MA", "sektor": "Prosedur Peradilan", "status": "Berlaku",
            "detail_url": "https://jdih.mahkamahagung.go.id/perma-2-2015",
            "content": (
                "Gugatan sederhana (Small Claims Court) adalah tata cara pemeriksaan di persidangan "
                "terhadap gugatan perdata dengan nilai gugatan materil paling banyak Rp500.000.000,00.\n\n"
                "Gugatan sederhana tidak dapat diajukan untuk: perkara yang penyelesaiannya dilakukan "
                "melalui pengadilan khusus; sengketa hak atas tanah.\n\n"
                "Penyelesaian gugatan sederhana paling lama 25 hari kerja sejak hari sidang pertama."
            ),
        },
        {
            "judul": "Peraturan Mahkamah Agung Nomor 13 Tahun 2016 tentang Tata Cara Penanganan Perkara Tindak Pidana oleh Korporasi",
            "nomor": "PERMA No. 13 Tahun 2016",
            "jenis": "Peraturan MA", "sektor": "Hukum Pidana Korporasi", "status": "Berlaku",
            "detail_url": "https://jdih.mahkamahagung.go.id/perma-13-2016",
            "content": (
                "Korporasi dapat dimintakan pertanggungjawaban pidana apabila tindak pidana dilakukan "
                "oleh pengurus korporasi dalam rangka pemenuhan maksud dan tujuan korporasi.\n\n"
                "Pidana yang dapat dijatuhkan kepada Korporasi: pidana denda; pidana tambahan berupa "
                "pembayaran uang pengganti; pencabutan izin usaha; penutupan korporasi; perampasan "
                "aset korporasi hasil tindak pidana."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  MAHKAMAH AGUNG SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    MahkamahAgungScraper().scrape()
