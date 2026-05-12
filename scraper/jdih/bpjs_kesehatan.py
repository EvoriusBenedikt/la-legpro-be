"""BPJS Kesehatan Scraper — JKN, SJSN, iuran, klaim"""
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class BPJSKesehatanScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("BPJS_Kesehatan")

    CURATED_CORPUS = [
        {
            "judul": "Undang-Undang Nomor 40 Tahun 2004 tentang Sistem Jaminan Sosial Nasional (SJSN)",
            "nomor": "UU No. 40 Tahun 2004",
            "jenis": "Undang-Undang", "sektor": "Jaminan Sosial", "status": "Berlaku",
            "detail_url": "https://bpjs-kesehatan.go.id/uu-40-2004",
            "content": (
                "Pasal 1 — Jaminan sosial adalah salah satu bentuk perlindungan sosial untuk menjamin seluruh "
                "rakyat agar dapat memenuhi kebutuhan dasar hidupnya yang layak.\n\n"
                "Pasal 19 — Jaminan kesehatan diselenggarakan secara nasional berdasarkan prinsip asuransi "
                "sosial dan prinsip ekuitas.\n\n"
                "Pasal 20 — Peserta jaminan kesehatan adalah setiap orang yang telah membayar iuran atau "
                "iurannya dibayar oleh Pemerintah.\n\n"
                "Pasal 22 — Manfaat jaminan kesehatan bersifat pelayanan perseorangan berupa pelayanan "
                "kesehatan yang mencakup pelayanan promotif, preventif, kuratif, dan rehabilitatif, termasuk "
                "obat dan bahan medis habis pakai yang diperlukan."
            ),
        },
        {
            "judul": "Peraturan Presiden Nomor 82 Tahun 2018 tentang Jaminan Kesehatan",
            "nomor": "Perpres No. 82 Tahun 2018",
            "jenis": "Peraturan Presiden", "sektor": "Jaminan Kesehatan", "status": "Berlaku",
            "detail_url": "https://bpjs-kesehatan.go.id/perpres-82-2018",
            "content": (
                "Pasal 4 — Setiap Penduduk wajib menjadi Peserta JKN (Jaminan Kesehatan Nasional).\n\n"
                "Pasal 16 — Iuran bagi Peserta Pekerja Penerima Upah (PPU) yang bekerja pada Pemberi Kerja "
                "selain penyelenggara negara sebesar 5% dari gaji atau upah per bulan, dengan ketentuan "
                "4% dibayar oleh Pemberi Kerja dan 1% dibayar oleh Peserta.\n\n"
                "Pasal 55 — Pemberi Kerja wajib mendaftarkan dirinya dan Pekerjanya sebagai Peserta JKN "
                "kepada BPJS Kesehatan dengan membayar iuran.\n\n"
                "Pasal 62 — Pemberi Kerja yang tidak mendaftarkan Pekerjanya kepada BPJS Kesehatan dikenai "
                "sanksi administratif berupa: teguran tertulis, denda, dan/atau tidak mendapat pelayanan publik."
            ),
        },
        {
            "judul": "Peraturan BPJS Kesehatan Nomor 1 Tahun 2014 tentang Penyelenggaraan Jaminan Kesehatan",
            "nomor": "Peraturan BPJS Kesehatan No. 1 Tahun 2014",
            "jenis": "Peraturan BPJS", "sektor": "Jaminan Kesehatan", "status": "Berlaku",
            "detail_url": "https://bpjs-kesehatan.go.id/peraturan-1-2014",
            "content": (
                "Pelayanan Kesehatan Tingkat Pertama (PKTP) meliputi pelayanan kesehatan non-spesialistik yang "
                "mencakup: administrasi pelayanan, pelayanan promotif dan preventif, pemeriksaan, pengobatan, "
                "dan konsultasi medis.\n\n"
                "Sistem rujukan berjenjang: Peserta harus mendapatkan pelayanan di Fasilitas Kesehatan Tingkat "
                "Pertama (FKTP) sebelum dirujuk ke Fasilitas Kesehatan Rujukan Tingkat Lanjutan (FKRTL).\n\n"
                "Peserta JKN berhak mendapat manfaat rawat inap di kelas sesuai haknya: Kelas I, II, atau III "
                "tergantung jenis kepesertaan dan besaran iuran."
            ),
        },
    ]

    def scrape(self, limit: int = 100):
        print(f"\n{'='*60}\n  BPJS KESEHATAN SCRAPER\n{'='*60}")
        injected = self._inject_curated(self.CURATED_CORPUS)
        print(f"  Done: {injected} items\n")

if __name__ == "__main__":
    BPJSKesehatanScraper().scrape()
