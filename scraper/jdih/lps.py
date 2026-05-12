"""LPS Scraper — Penjaminan simpanan bank, premi, resolusi bank gagal"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class LPSScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("LPS")

    CURATED_CORPUS = [
        {
            "judul": "Undang-Undang Nomor 24 Tahun 2004 tentang Lembaga Penjamin Simpanan",
            "nomor": "UU No. 24 Tahun 2004",
            "jenis": "Undang-Undang", "sektor": "Penjaminan Simpanan", "status": "Berlaku",
            "detail_url": "https://lps.go.id/uu-24-2004",
            "content": (
                "Pasal 1 — LPS adalah lembaga yang menjamin simpanan nasabah bank dan turut aktif "
                "dalam memelihara stabilitas sistem perbankan.\n\n"
                "Pasal 11 — Nilai simpanan yang dijamin untuk setiap nasabah penyimpan pada satu bank "
                "paling banyak sebesar Rp2.000.000.000 (dua miliar rupiah).\n\n"
                "Pasal 13 — Simpanan yang dijamin: giro, deposito, sertifikat deposito, tabungan.\n\n"
                "Pasal 37 — LPS membayar klaim penjaminan dalam waktu paling lama 90 hari kerja."
            ),
        },
        {
            "judul": "Peraturan LPS tentang Premi Penjaminan Simpanan",
            "nomor": "PLPS No. 2 Tahun 2014",
            "jenis": "Peraturan LPS", "sektor": "Penjaminan Simpanan", "status": "Berlaku",
            "detail_url": "https://lps.go.id/plps-2-2014",
            "content": (
                "Premi penjaminan simpanan dibayar bank setiap 6 bulan sebesar 0,1% dari rata-rata "
                "saldo bulanan total simpanan. Bank yang terlambat membayar dikenakan denda 2% per bulan.\n\n"
                "Simpanan hanya dijamin jika tingkat bunga tidak melebihi Tingkat Bunga Penjaminan (TBP) "
                "yang ditetapkan LPS setiap 2 bulan."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  LPS SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    LPSScraper().scrape()
