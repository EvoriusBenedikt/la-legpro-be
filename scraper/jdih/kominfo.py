"""Kominfo / Komdigi JDIH Scraper — UU ITE, PDP, Sistem Elektronik"""
import os, sys, time
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class KominfoScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Kominfo")

    CURATED_CORPUS = [
        {
            "judul": "Undang-Undang Nomor 11 Tahun 2008 tentang Informasi dan Transaksi Elektronik (UU ITE)",
            "nomor": "UU No. 11 Tahun 2008",
            "jenis": "Undang-Undang", "sektor": "Teknologi Informasi", "status": "Berlaku (diubah UU 19/2016)",
            "detail_url": "https://jdih.komdigi.go.id/uu-11-2008",
            "content": (
                "Pasal 1 — Transaksi Elektronik adalah perbuatan hukum yang dilakukan dengan menggunakan Komputer, "
                "jaringan Komputer, dan/atau media elektronik lainnya.\n\n"
                "Pasal 27 — Setiap Orang dengan sengaja dan tanpa hak mendistribusikan dan/atau mentransmisikan "
                "dan/atau membuat dapat diaksesnya Informasi Elektronik dan/atau Dokumen Elektronik yang memiliki "
                "muatan yang melanggar kesusilaan diancam pidana penjara paling lama 6 tahun.\n\n"
                "Pasal 40 — Pemerintah melindungi kepentingan umum dari segala jenis gangguan sebagai akibat "
                "penyalahgunaan Informasi Elektronik dan Transaksi Elektronik yang mengganggu ketertiban umum.\n\n"
                "UU ITE mengatur: tanda tangan elektronik, kontrak elektronik, sertifikasi keandalan, "
                "penyelesaian sengketa, dan tindak pidana di bidang teknologi informasi."
            ),
        },
        {
            "judul": "Undang-Undang Nomor 27 Tahun 2022 tentang Pelindungan Data Pribadi (UU PDP)",
            "nomor": "UU No. 27 Tahun 2022",
            "jenis": "Undang-Undang", "sektor": "Pelindungan Data Pribadi", "status": "Berlaku",
            "detail_url": "https://jdih.komdigi.go.id/uu-27-2022",
            "content": (
                "Pasal 1 — Data Pribadi adalah data tentang orang perseorangan yang teridentifikasi "
                "atau dapat diidentifikasi secara tersendiri atau dikombinasi dengan informasi lainnya.\n\n"
                "Pasal 16 — Pemrosesan Data Pribadi harus memenuhi dasar pemrosesan: persetujuan eksplisit, "
                "perjanjian, kewajiban hukum, kepentingan vital, tugas dalam rangka kepentingan umum, atau "
                "kepentingan yang sah.\n\n"
                "Pasal 57 — Pengendali Data Pribadi yang melanggar ketentuan dikenai sanksi administratif berupa: "
                "peringatan tertulis, penghentian sementara pemrosesan, penghapusan data, dan denda administratif "
                "paling tinggi 2% dari pendapatan tahunan atau penerimaan tahunan terhadap variabel pelanggaran.\n\n"
                "Pasal 67 — Setiap Orang yang dengan sengaja memperoleh atau mengumpulkan Data Pribadi yang bukan "
                "miliknya dengan maksud menguntungkan diri sendiri dipidana penjara paling lama 5 tahun."
            ),
        },
        {
            "judul": "Peraturan Pemerintah Nomor 71 Tahun 2019 tentang Penyelenggaraan Sistem dan Transaksi Elektronik",
            "nomor": "PP No. 71 Tahun 2019",
            "jenis": "Peraturan Pemerintah", "sektor": "Sistem Elektronik", "status": "Berlaku",
            "detail_url": "https://jdih.komdigi.go.id/pp-71-2019",
            "content": (
                "Pasal 2 — Penyelenggara Sistem Elektronik wajib memastikan sistem elektroniknya dapat beroperasi "
                "secara aman, andal, dan bertanggung jawab.\n\n"
                "Pasal 21 — Penyelenggara Sistem Elektronik lingkup privat wajib menempatkan Pusat Data dan "
                "Pusat Pemulihan Bencana di wilayah Indonesia untuk kepentingan penegakan hukum, perlindungan, "
                "dan penegakan kedaulatan negara atas Data Strategis.\n\n"
                "Pasal 99 — Penyelenggara Sistem Elektronik yang melanggar kewajiban dalam PP ini dikenai sanksi "
                "administratif berupa: teguran tertulis, denda administratif, penghentian sementara, "
                "pemutusan akses, atau pencabutan izin."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  KOMINFO SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    KominfoScraper().scrape()
