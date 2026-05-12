"""PPATK Scraper — Anti-money laundering, KYC, TPPU, pelaporan transaksi"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class PPATKScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("PPATK")

    CURATED_CORPUS = [
        {
            "judul": "Undang-Undang Nomor 8 Tahun 2010 tentang Pencegahan dan Pemberantasan Tindak Pidana Pencucian Uang (TPPU)",
            "nomor": "UU No. 8 Tahun 2010",
            "jenis": "Undang-Undang", "sektor": "Anti-Pencucian Uang", "status": "Berlaku",
            "detail_url": "https://ppatk.go.id/uu-8-2010",
            "content": (
                "Pasal 1 — Pencucian Uang adalah segala perbuatan yang memenuhi unsur-unsur tindak pidana "
                "sesuai dengan ketentuan dalam Undang-Undang ini.\n\n"
                "Pasal 3 — Setiap Orang yang menempatkan, mentransfer, mengalihkan, membelanjakan, "
                "membayarkan, menghibahkan, menitipkan, membawa ke luar negeri, mengubah bentuk, "
                "menukarkan dengan mata uang atau surat berharga atau perbuatan lain atas Harta Kekayaan "
                "yang diketahuinya atau patut diduganya merupakan hasil tindak pidana dipidana penjara "
                "paling lama 20 tahun.\n\n"
                "Pasal 17 — Pihak Pelapor wajib: menerapkan Prinsip Mengenali Pengguna Jasa (KYC/CDD); "
                "melaporkan Transaksi Keuangan Mencurigakan (TKM); melaporkan Transaksi Keuangan Tunai "
                "(TKT) di atas Rp500 juta.\n\n"
                "Pihak Pelapor meliputi: Penyedia Jasa Keuangan (PJK), Penyedia Barang dan/atau Jasa "
                "lain (PBJ) seperti agen properti, dealer otomotif, pedagang emas permata, notaris, "
                "pengacara, dan akuntan."
            ),
        },
        {
            "judul": "Peraturan PPATK tentang Prinsip Mengenali Pengguna Jasa (Know Your Customer / KYC)",
            "nomor": "Peraturan PPATK No. 1 Tahun 2021",
            "jenis": "Peraturan PPATK", "sektor": "KYC/CDD", "status": "Berlaku",
            "detail_url": "https://ppatk.go.id/peraturan-kyc-2021",
            "content": (
                "Customer Due Diligence (CDD) atau Prinsip Mengenali Pengguna Jasa mencakup:\n"
                "a. Identifikasi pengguna jasa dan/atau beneficial owner;\n"
                "b. Verifikasi identitas pengguna jasa menggunakan dokumen yang sah;\n"
                "c. Pemahaman tujuan hubungan usaha;\n"
                "d. Pemantauan transaksi secara berkelanjutan.\n\n"
                "Enhanced Due Diligence (EDD) wajib diterapkan untuk:\n"
                "- Nasabah Politically Exposed Person (PEP);\n"
                "- Transaksi lintas batas negara berisiko tinggi;\n"
                "- Nasabah dengan profil berisiko tinggi.\n\n"
                "Transaksi Keuangan Mencurigakan (TKM) harus dilaporkan ke PPATK dalam 3 hari kerja "
                "setelah diketahui."
            ),
        },
        {
            "judul": "Peraturan Bank Indonesia tentang Anti Pencucian Uang dan Pencegahan Pendanaan Terorisme (APU PPT)",
            "nomor": "PBI No. 12/3/PBI/2010",
            "jenis": "Peraturan Bank Indonesia", "sektor": "APU-PPT", "status": "Berlaku",
            "detail_url": "https://ppatk.go.id/pbi-apu-ppt",
            "content": (
                "Bank wajib menerapkan Program APU PPT (Anti Pencucian Uang dan Pencegahan Pendanaan "
                "Terorisme) yang mencakup:\n"
                "1. Pengawasan aktif Direksi dan Dewan Komisaris;\n"
                "2. Kebijakan dan prosedur CDD/KYC;\n"
                "3. Sistem pengendalian intern;\n"
                "4. Sistem informasi manajemen;\n"
                "5. Sumber daya manusia dan pelatihan.\n\n"
                "Bank dilarang membuka rekening atau melakukan hubungan usaha dengan nasabah yang "
                "menggunakan nama fiktif atau anonim (shell banking dilarang)."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  PPATK SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    PPATKScraper().scrape()
