"""DPR JDIH Scraper — Foundational UU: Perseroan Terbatas, Pasar Modal, Perbankan, OJK"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class DPRScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("DPR")

    CURATED_CORPUS = [
        {
            "judul": "Undang-Undang Nomor 40 Tahun 2007 tentang Perseroan Terbatas",
            "nomor": "UU No. 40 Tahun 2007",
            "jenis": "Undang-Undang", "sektor": "Hukum Perusahaan", "status": "Berlaku",
            "detail_url": "https://jdih.dpr.go.id/uu-40-2007",
            "content": (
                "Pasal 1 — Perseroan Terbatas (PT) adalah badan hukum yang merupakan persekutuan modal, "
                "didirikan berdasarkan perjanjian, melakukan kegiatan usaha dengan modal dasar yang "
                "seluruhnya terbagi dalam saham.\n\n"
                "Pasal 79 — RUPS (Rapat Umum Pemegang Saham) adalah organ Perseroan yang mempunyai "
                "wewenang yang tidak diberikan kepada Direksi atau Dewan Komisaris.\n\n"
                "Pasal 97 — Direksi bertanggung jawab atas pengurusan Perseroan. Setiap anggota Direksi "
                "bertanggung jawab penuh secara pribadi atas kerugian Perseroan apabila yang bersangkutan "
                "bersalah atau lalai menjalankan tugasnya.\n\n"
                "Pasal 108 — Dewan Komisaris melakukan pengawasan atas kebijakan pengurusan, jalannya "
                "pengurusan pada umumnya oleh Direksi."
            ),
        },
        {
            "judul": "Undang-Undang Nomor 8 Tahun 1995 tentang Pasar Modal",
            "nomor": "UU No. 8 Tahun 1995",
            "jenis": "Undang-Undang", "sektor": "Pasar Modal", "status": "Berlaku",
            "detail_url": "https://jdih.dpr.go.id/uu-8-1995",
            "content": (
                "Pasal 1 — Pasar Modal adalah kegiatan yang bersangkutan dengan Penawaran Umum dan "
                "perdagangan Efek, Perusahaan Publik yang berkaitan dengan Efek yang diterbitkannya, "
                "serta lembaga dan profesi yang berkaitan dengan Efek.\n\n"
                "Pasal 95 — Setiap Pihak yang memiliki Informasi Orang Dalam dilarang melakukan "
                "pembelian atau penjualan atas Efek Emiten atau Perusahaan Publik dimaksud (Insider Trading).\n\n"
                "Reksa Dana adalah wadah yang dipergunakan untuk menghimpun dana dari masyarakat pemodal "
                "untuk selanjutnya diinvestasikan dalam portofolio Efek oleh Manajer Investasi."
            ),
        },
        {
            "judul": "Undang-Undang Nomor 10 Tahun 1998 tentang Perubahan atas UU No. 7 Tahun 1992 tentang Perbankan",
            "nomor": "UU No. 10 Tahun 1998",
            "jenis": "Undang-Undang", "sektor": "Perbankan", "status": "Berlaku",
            "detail_url": "https://jdih.dpr.go.id/uu-10-1998",
            "content": (
                "Pasal 1 — Bank adalah badan usaha yang menghimpun dana dari masyarakat dalam bentuk "
                "simpanan dan menyalurkannya kepada masyarakat dalam bentuk kredit dan/atau bentuk-bentuk "
                "lainnya dalam rangka meningkatkan taraf hidup rakyat banyak.\n\n"
                "Pasal 3 — Fungsi utama perbankan Indonesia adalah sebagai penghimpun dan penyalur "
                "dana masyarakat (intermediasi).\n\n"
                "Pasal 37 — Bank Indonesia dapat mencabut izin usaha Bank apabila: modal bank menjadi "
                "berkurang dari jumlah minimum; tidak menyampaikan laporan; melakukan pelanggaran ketentuan."
            ),
        },
        {
            "judul": "Undang-Undang Nomor 21 Tahun 2011 tentang Otoritas Jasa Keuangan (OJK)",
            "nomor": "UU No. 21 Tahun 2011",
            "jenis": "Undang-Undang", "sektor": "Otoritas Jasa Keuangan", "status": "Berlaku",
            "detail_url": "https://jdih.dpr.go.id/uu-21-2011",
            "content": (
                "Pasal 1 — OJK adalah lembaga yang independen dan bebas dari campur tangan pihak lain, "
                "yang mempunyai fungsi, tugas, dan wewenang pengaturan, pengawasan, pemeriksaan, dan "
                "penyidikan.\n\n"
                "Pasal 6 — OJK melaksanakan tugas pengaturan dan pengawasan terhadap: kegiatan jasa "
                "keuangan di sektor perbankan; kegiatan jasa keuangan di sektor pasar modal; kegiatan "
                "jasa keuangan di sektor perasuransian, dana pensiun, lembaga pembiayaan, dan lembaga "
                "jasa keuangan lainnya.\n\n"
                "Pasal 9 — OJK berwenang: menetapkan peraturan perundang-undangan; memberikan dan "
                "mencabut izin usaha; menetapkan sanksi administratif."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  DPR JDIH SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    DPRScraper().scrape()
