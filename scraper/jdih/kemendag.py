"""Kemendag Scraper — Perdagangan, e-commerce, perlindungan konsumen"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class KemendagScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Kemendag")

    CURATED_CORPUS = [
        {
            "judul": "Undang-Undang Nomor 7 Tahun 2014 tentang Perdagangan",
            "nomor": "UU No. 7 Tahun 2014",
            "jenis": "Undang-Undang", "sektor": "Perdagangan", "status": "Berlaku",
            "detail_url": "https://jdih.kemendag.go.id/uu-7-2014",
            "content": (
                "Pasal 1 — Perdagangan adalah tatanan kegiatan yang terkait dengan transaksi Barang dan/atau "
                "Jasa di dalam negeri dan melampaui batas wilayah negara dengan tujuan pengalihan hak atas "
                "Barang dan/atau Jasa untuk memperoleh imbalan atau kompensasi.\n\n"
                "Pasal 65 — Setiap Pelaku Usaha yang memperdagangkan Barang dan/atau Jasa dengan menggunakan "
                "sistem elektronik wajib menyediakan data dan/atau informasi secara lengkap dan benar.\n\n"
                "Pasal 66 — Setiap Pelaku Usaha yang memperdagangkan Barang dan/atau Jasa dengan menggunakan "
                "sistem elektronik dilarang: memperdagangkan Barang dan/atau Jasa yang dilarang untuk "
                "diperdagangkan, memuat informasi yang tidak benar, dan melanggar hak kekayaan intelektual."
            ),
        },
        {
            "judul": "Undang-Undang Nomor 8 Tahun 1999 tentang Perlindungan Konsumen",
            "nomor": "UU No. 8 Tahun 1999",
            "jenis": "Undang-Undang", "sektor": "Perlindungan Konsumen", "status": "Berlaku",
            "detail_url": "https://jdih.kemendag.go.id/uu-8-1999",
            "content": (
                "Pasal 4 — Hak konsumen: hak atas kenyamanan, keamanan, dan keselamatan; hak memilih barang; "
                "hak atas informasi yang benar, jelas, dan jujur; hak untuk didengar; hak untuk mendapat "
                "advokasi; hak mendapat pembinaan; hak untuk diperlakukan/dilayani secara benar dan jujur; "
                "hak untuk mendapat kompensasi/ganti rugi.\n\n"
                "Pasal 7 — Kewajiban pelaku usaha: beritikad baik; memberikan informasi yang benar, jelas, "
                "dan jujur; memperlakukan konsumen secara benar dan jujur; menjamin mutu barang dan/atau "
                "jasa; memberi kesempatan kepada konsumen untuk menguji dan/atau mencoba barang; memberi "
                "kompensasi/ganti rugi atas kerugian akibat penggunaan, pemakaian, dan pemanfaatan barang.\n\n"
                "Pasal 19 — Pelaku usaha bertanggung jawab memberikan ganti rugi atas kerusakan, pencemaran, "
                "dan/atau kerugian konsumen akibat mengkonsumsi barang dan/atau jasa. Ganti rugi dapat berupa "
                "pengembalian uang, penggantian barang, perawatan kesehatan, dan/atau pemberian santunan."
            ),
        },
        {
            "judul": "Peraturan Menteri Perdagangan Nomor 50 Tahun 2020 tentang Ketentuan Perizinan Usaha, Periklanan, Pembinaan, dan Pengawasan Pelaku Usaha dalam Perdagangan Melalui Sistem Elektronik",
            "nomor": "Permendag No. 50 Tahun 2020",
            "jenis": "Peraturan Menteri", "sektor": "E-Commerce", "status": "Berlaku",
            "detail_url": "https://jdih.kemendag.go.id/permendag-50-2020",
            "content": (
                "Perdagangan Melalui Sistem Elektronik (PMSE) atau e-commerce wajib memiliki izin usaha "
                "sesuai ketentuan peraturan perundang-undangan.\n\n"
                "Pelaku Usaha PMSE wajib: menyediakan layanan pengaduan konsumen; memberikan informasi "
                "produk yang akurat; menyediakan mekanisme pengembalian barang (retur); memastikan "
                "keamanan transaksi elektronik.\n\n"
                "Marketplace dilarang: menjual barang yang dilarang peredaran; memperdagangkan data "
                "pribadi konsumen; memfasilitasi penjual yang tidak memiliki izin untuk produk tertentu."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  KEMENDAG SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    KemendagScraper().scrape()
