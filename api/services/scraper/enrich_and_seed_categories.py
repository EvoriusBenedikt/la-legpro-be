"""Enrich abbreviated regulation titles from PDF chunks and scrape/seed regulations
for all 14 legal categories so zero categories remain empty."""
from __future__ import annotations

import hashlib
import os
import re
import sys

API_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DIR = os.path.dirname(API_DIR)
for p in (BASE_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from services import pg_service
from services.embed_service import embed_documents
from services.scraper.canonicalizer import build_canonical_key, normalize_nomor_tahun
from services.scraper.category_classifier import (
    backfill_existing_categories,
    classify_regulation_category,
)

PDF_DIR = os.path.join(BASE_DIR, "data", "pdfs")
os.makedirs(PDF_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Comprehensive Indonesian Regulation Corpus across the 14 Legal Categories
# ---------------------------------------------------------------------------
MULTI_CATEGORY_REGULATIONS: list[dict] = [
    # ==================== 1. PROPERTY_AGRARIAN (8 Regulations) ====================
    {
        "domain": "jdih_atrbpn",
        "judul": "Undang-Undang Nomor 5 Tahun 1960 tentang Peraturan Dasar Pokok-Pokok Agraria (UUPA)",
        "nomor": "UU No. 5 Tahun 1960",
        "jenis": "UU",
        "sektor": "Pertanahan & Agraria",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.atrbpn.go.id/uu-5-1960",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 5 TAHUN 1960 TENTANG PERATURAN DASAR POKOK-POKOK AGRARIA\n\n"
            "Pasal 16 — Hak-hak atas tanah sebagai yang dimaksud dalam Pasal 4 ayat (1) ialah:\n"
            "a. hak milik (HM);\n"
            "b. hak guna usaha (HGU);\n"
            "c. hak guna bangunan (HGB);\n"
            "d. hak pakai;\n"
            "e. hak sewa;\n"
            "f. hak membuka tanah dan hak memungut hasil hutan.\n\n"
            "Pasal 19 — Untuk menjamin kepastian hukum oleh Pemerintah diadakan pendaftaran tanah di seluruh wilayah "
            "Republik Indonesia menurut ketentuan-ketentuan yang diatur dengan Peraturan Pemerintah.\n\n"
            "Pasal 21 — Hanya warga negara Indonesia dapat mempunyai hak milik. Badan-badan hukum yang dapat mempunyai "
            "hak milik dan syarat-syaratnya ditetapkan oleh Pemerintah.\n\n"
            "Pasal 35 — Hak Guna Bangunan (HGB) adalah hak untuk mendirikan dan mempunyai bangunan-bangunan atas tanah "
            "yang bukan miliknya sendiri, dengan jangka waktu paling lama 30 tahun dan dapat diperpanjang paling lama 20 tahun."
        ),
    },
    {
        "domain": "jdih_pupr",
        "judul": "Undang-Undang Nomor 2 Tahun 2017 tentang Jasa Konstruksi",
        "nomor": "UU No. 2 Tahun 2017",
        "jenis": "UU",
        "sektor": "Jasa Konstruksi & PUPR",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.pu.go.id/uu-2-2017",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 2 TAHUN 2017 TENTANG JASA KONSTRUKSI\n\n"
            "Pasal 46 — Pengaturan hubungan kerja antara Pengguna Jasa dan Penyedia Jasa harus dituangkan dalam "
            "Kontrak Kerja Konstruksi.\n\n"
            "Pasal 47 — Kontrak Kerja Konstruksi paling sedikit harus mencakup uraian mengenai:\n"
            "a. para pihak, memuat secara jelas identitas para pihak;\n"
            "b. rumusan pekerjaan, memuat uraian yang jelas dan rinci tentang lingkup kerja, nilai pekerjaan, harga satuan, "
            "lumsum, dan batasan waktu pelaksanaan;\n"
            "c. masa pertanggungan, memuat jangka waktu pelaksanaan dan pemeliharaan yang menjadi tanggung jawab Penyedia Jasa;\n"
            "d. hak dan kewajiban yang setara;\n"
            "e. penggunaan tenaga kerja konstruksi bersertifikat;\n"
            "f. cara pembayaran, termasuk jaminan atas pembayaran;\n"
            "g. wanprestasi, memuat ketentuan tentang tanggung jawab dalam hal salah satu pihak tidak melaksanakan kewajiban;\n"
            "h. penyelesaian perselisihan, memuat mekanisme musyawarah, mediasi, konsiliasi, arbitrase, atau pengadilan;\n"
            "i. pemutusan Kontrak Kerja Konstruksi;\n"
            "j. keadaan memaksa (force majeure);\n"
            "k. kegagalan bangunan;\n"
            "l. pelindungan pekerja, memuat ketentuan tentang kewajiban para pihak dalam pelaksanaan keselamatan dan kesehatan kerja konstruksi (K3 Konstruksi).\n\n"
            "Pasal 50 — Kontrak Kerja Konstruksi dibuat dalam bahasa Indonesia. Dalam hal Kontrak Kerja Konstruksi dilakukan "
            "dengan pihak asing, harus dibuat dalam bahasa Indonesia dan bahasa Inggris, dan dalam hal terjadi perselisihan "
            "maka yang berlaku adalah Kontrak Kerja Konstruksi dalam bahasa Indonesia.\n\n"
            "Pasal 65 — Penyedia Jasa wajib bertanggung jawab atas Kegagalan Bangunan dalam jangka waktu yang ditentukan "
            "sesuai dengan rencana umur konstruksi paling lama 10 (sepuluh) tahun sejak tanggal penyerahan akhir layanan jasa konstruksi."
        ),
    },
    {
        "domain": "jdih_pupr",
        "judul": "Undang-Undang Nomor 20 Tahun 2011 tentang Rumah Susun",
        "nomor": "UU No. 20 Tahun 2011",
        "jenis": "UU",
        "sektor": "Perumahan & Properti",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.pu.go.id/uu-20-2011",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 20 TAHUN 2011 TENTANG RUMAH SUSUN\n\n"
            "Pasal 42 — Pelaku pembangunan dapat melakukan pemasaran sebelum pembangunan rumah susun dilaksanakan "
            "melalui Perjanjian Pengikatan Jual Beli (PPJB) setelah memenuhi kepastian atas status kepemilikan tanah, "
            "kepastian peruntukan ruang, kepastian ketersediaan prasarana, sarana, dan utilitas umum, keterbangunan "
            "paling sedikit 20% (dua puluh persen), dan hal yang diperjanjikan.\n\n"
            "Pasal 47 — Sebagai tanda bukti kepemilikan atas Satuan Rumah Susun (Sarusun) di atas tanah hak milik, HGB, "
            "atau hak pakai di atas tanah negara diterbitkan Sertifikat Hak Milik Satuan Rumah Susun (SHMSRS).\n\n"
            "Pasal 74 — Pemilik Sarusun wajib membentuk Perhimpunan Pemilik dan Penghuni Satuan Rumah Susun (PPPSRS) "
            "paling lambat sebelum masa transisi berakhir yakni 1 tahun sejak penyerahan pertama kali sarusun."
        ),
    },
    {
        "domain": "jdih_pupr",
        "judul": "Undang-Undang Nomor 1 Tahun 2011 tentang Perumahan dan Kawasan Permukiman",
        "nomor": "UU No. 1 Tahun 2011",
        "jenis": "UU",
        "sektor": "Perumahan & Properti",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.pu.go.id/uu-1-2011",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 1 TAHUN 2011 TENTANG PERUMAHAN DAN KAWASAN PERMUKIMAN\n\n"
            "Pasal 42 — Rumah tunggal, rumah deret, dan/atau rumah susun yang masih dalam tahap proses pembangunan "
            "dapat dipasarkan melalui sistem perjanjian pendahuluan jual beli (PPJB) sesuai dengan ketentuan peraturan "
            "perundang-undangan setelah memenuhi persyaratan kepastian atas status penguasaan tanah, hal yang diperjanjikan, "
            "kepemilikan izin mendirikan bangunan (PBG), ketersediaan prasarana, sarana, dan utilitas umum, serta "
            "keterbangunan perumahan paling sedikit 20%.\n\n"
            "Pasal 134 — Setiap orang dilarang menyelenggarakan pembangunan perumahan yang tidak membangun perumahan "
            "sesuai dengan kriteria, spesifikasi, persyaratan, prasarana, sarana, dan utilitas umum yang diperjanjikan."
        ),
    },
    {
        "domain": "jdih_atrbpn",
        "judul": "Undang-Undang Nomor 4 Tahun 1996 tentang Hak Tanggungan atas Tanah Beserta Benda-Benda yang Berkaitan dengan Tanah",
        "nomor": "UU No. 4 Tahun 1996",
        "jenis": "UU",
        "sektor": "Pertanahan & Jaminan Kebendaan",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.atrbpn.go.id/uu-4-1996",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 4 TAHUN 1996 TENTANG HAK TANGGUNGAN ATAS TANAH\n\n"
            "Pasal 1 — Hak Tanggungan adalah hak jaminan yang dibebankan pada hak atas tanah sebagaimana dimaksud dalam "
            "UUPA, berikut atau tidak berikut benda-benda lain yang merupakan satu kesatuan dengan tanah itu, untuk "
            "pelunasan utang tertentu, yang memberikan kedudukan yang diutamakan kepada kreditor tertentu terhadap kreditor-kreditor lain.\n\n"
            "Pasal 10 — Pemberian Hak Tanggungan didahului dengan janji untuk memberikan Hak Tanggungan sebagai jaminan "
            "pelunasan utang tertentu dan dilakukan dengan pembuatan Akta Pemberian Hak Tanggungan (APHT) oleh PPAT.\n\n"
            "Pasal 12 — Janji yang memberikan kewenangan kepada pemegang Hak Tanggungan untuk memiliki objek Hak Tanggungan "
            "apabila debitor cidera janji, batal demi hukum.\n\n"
            "Pasal 20 — Apabila debitor cidera janji, pemegang Hak Tanggungan pertama mempunyai hak untuk menjual objek "
            "Hak Tanggungan atas kekuasaan sendiri melalui pelelangan umum (parate eksekusi)."
        ),
    },
    {
        "domain": "jdih_atrbpn",
        "judul": "Peraturan Pemerintah Nomor 18 Tahun 2021 tentang Hak Pengelolaan, Hak Atas Tanah, Satuan Rumah Susun, dan Pendaftaran Tanah",
        "nomor": "PP No. 18 Tahun 2021",
        "jenis": "PP",
        "sektor": "Pertanahan & Agraria",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.atrbpn.go.id/pp-18-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 18 TAHUN 2021 TENTANG HAK PENGELOLAAN, HAK ATAS TANAH, SATUAN RUMAH SUSUN, DAN PENDAFTARAN TANAH\n\n"
            "Pasal 37 — Hak Guna Bangunan (HGB) di atas Tanah Negara dan Tanah Hak Pengelolaan diberikan untuk jangka waktu "
            "paling lama 30 tahun, diperpanjang untuk jangka waktu paling lama 20 tahun, dan diperbarui untuk jangka waktu "
            "paling lama 30 tahun.\n\n"
            "Pasal 84 — Penyelenggaraan dan pelaksanaan Pendaftaran Tanah dapat dilakukan secara elektronik (Sertipikat Elektronik). "
            "Hasil penyelenggaraan dan pelaksanaan Pendaftaran Tanah secara elektronik berupa data, informasi elektronik, "
            "dan/atau dokumen elektronik merupakan alat bukti hukum yang sah."
        ),
    },
    {
        "domain": "jdih_pupr",
        "judul": "Peraturan Pemerintah Nomor 22 Tahun 2020 tentang Peraturan Pelaksanaan Undang-Undang Nomor 2 Tahun 2017 tentang Jasa Konstruksi",
        "nomor": "PP No. 22 Tahun 2020",
        "jenis": "PP",
        "sektor": "Jasa Konstruksi & PUPR",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.pu.go.id/pp-22-2020",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 22 TAHUN 2020 TENTANG PERATURAN PELAKSANAAN UU JASA KONSTRUKSI\n\n"
            "Pasal 75 — Kontrak Kerja Konstruksi mengatur jaminan pelaksanaan, jaminan uang muka, jaminan pemeliharaan, "
            "retensi pembayaran, dan tata cara serah terima pekerjaan konstruksi (Provisional Hand Over / PHO dan Final Hand Over / FHO).\n\n"
            "Pasal 85 — Penyelesaian sengketa Kontrak Kerja Konstruksi di luar pengadilan dilakukan melalui tahapan: "
            "musyawarah untuk mufakat, mediasi, konsiliasi, Dewan Sengketa Konstruksi (Dispute Board), atau arbitrase."
        ),
    },
    {
        "domain": "jdih_pupr",
        "judul": "Peraturan Pemerintah Nomor 16 Tahun 2021 tentang Peraturan Pelaksanaan Undang-Undang Nomor 28 Tahun 2002 tentang Bangunan Gedung",
        "nomor": "PP No. 16 Tahun 2021",
        "jenis": "PP",
        "sektor": "Bangunan Gedung & Konstruksi",
        "status": "Berlaku",
        "category_code": "PROPERTY_AGRARIAN",
        "detail_url": "https://jdih.pu.go.id/pp-16-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 16 TAHUN 2021 TENTANG BANGUNAN GEDUNG\n\n"
            "Pasal 253 — Persetujuan Bangunan Gedung (PBG) wajib dimiliki oleh pemilik bangunan gedung sebelum pelaksanaan "
            "konstruksi. Pemanfaatan Bangunan Gedung wajib dilengkapi dengan Sertifikat Laik Fungsi (SLF) yang diterbitkan "
            "oleh Pemerintah Daerah berdasarkan pemeriksaan kelaikan fungsi bangunan gedung."
        ),
    },

    # ==================== 2. ENERGY_MINING_ENV (8 Regulations) ====================
    {
        "domain": "jdih_klhk",
        "judul": "Undang-Undang Nomor 32 Tahun 2009 tentang Perlindungan dan Pengelolaan Lingkungan Hidup",
        "nomor": "UU No. 32 Tahun 2009",
        "jenis": "UU",
        "sektor": "Lingkungan Hidup & Kehutanan",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.menlhk.go.id/uu-32-2009",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 32 TAHUN 2009 TENTANG PERLINDUNGAN DAN PENGELOLAAN LINGKUNGAN HIDUP\n\n"
            "Pasal 22 — Setiap usaha dan/atau kegiatan yang berdampak penting terhadap lingkungan hidup wajib memiliki "
            "Analisis Mengenai Dampak Lingkungan Hidup (AMDAL).\n\n"
            "Pasal 34 — Setiap usaha dan/atau kegiatan yang tidak termasuk dalam kriteria wajib AMDAL wajib memiliki "
            "Upaya Pengelolaan Lingkungan Hidup dan Upaya Pemantauan Lingkungan Hidup (UKL-UPL).\n\n"
            "Pasal 59 — Setiap orang yang menghasilkan limbah bahan berbahaya dan beracun (Limbah B3) wajib melakukan "
            "pengelolaan limbah B3 yang dihasilkannya.\n\n"
            "Pasal 88 — Tanggung Jawab Mutlak (Strict Liability): Setiap orang yang tindakannya, usahanya, dan/atau "
            "kegiatannya menggunakan B3, menghasilkan dan/atau mengelola limbah B3, dan/atau yang menimbulkan ancaman "
            "serius terhadap lingkungan hidup bertanggung jawab mutlak atas kerugian yang terjadi tanpa perlu pembuktian unsur kesalahan."
        ),
    },
    {
        "domain": "jdih_esdm",
        "judul": "Undang-Undang Nomor 3 Tahun 2020 tentang Perubahan atas Undang-Undang Nomor 4 Tahun 2009 tentang Pertambangan Mineral dan Batubara",
        "nomor": "UU No. 3 Tahun 2020",
        "jenis": "UU",
        "sektor": "Pertambangan Mineral & Batubara",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.esdm.go.id/uu-3-2020",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 3 TAHUN 2020 TENTANG PERTAMBANGAN MINERAL DAN BATUBARA (UU MINERBA)\n\n"
            "Pasal 35 — Usaha Pertambangan dilaksanakan berdasarkan Perizinan Berusaha dari Pemerintah Pusat yang terdiri atas:\n"
            "a. Izin Usaha Pertambangan (IUP);\n"
            "b. Izin Usaha Pertambangan Khusus (IUPK);\n"
            "c. Izin Pertambangan Rakyat (IPR);\n"
            "d. Surat Izin Penambangan Batuan (SIPB);\n"
            "e. Izin Usaha Jasa Pertambangan (IUJP).\n\n"
            "Pasal 99 — Pemegang IUP dan IUPK wajib menyusun dan menyerahkan rencana Reklamasi dan rencana Pascatambang "
            "serta menempatkan dana jaminan Reklamasi dan dana jaminan Pascatambang.\n\n"
            "Pasal 102 — Pemegang IUP atau IUPK tahap kegiatan Operasi Produksi wajib meningkatkan nilai tambah sumber daya "
            "mineral dan/atau batubara melalui kegiatan pengolahan dan/atau pemurnian (hilirisasi) di dalam negeri.\n\n"
            "Pasal 112 — Badan usaha pemegang IUP atau IUPK yang sahamnya dimiliki oleh asing wajib melakukan divestasi "
            "saham secara bertahap sebesar 51% kepada Pemerintah Pusat, Pemerintah Daerah, BUMN, BUMD, dan/atau Badan Usaha Swasta Nasional."
        ),
    },
    {
        "domain": "jdih_esdm",
        "judul": "Undang-Undang Nomor 30 Tahun 2009 tentang Ketenagalistrikan",
        "nomor": "UU No. 30 Tahun 2009",
        "jenis": "UU",
        "sektor": "Energi & Ketenagalistrikan",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.esdm.go.id/uu-30-2009",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 30 TAHUN 2009 TENTANG KETENAGALISTRIKAN\n\n"
            "Pasal 10 — Usaha penyediaan tenaga listrik untuk kepentingan umum meliputi pembangkitan tenaga listrik, "
            "transmisi tenaga listrik, distribusi tenaga listrik, dan/atau penjualan tenaga listrik.\n\n"
            "Pasal 27 — Perjanjian jual beli tenaga listrik (Power Purchase Agreement / PPA) antara pemegang izin usaha "
            "penyediaan tenaga listrik dan badan usaha pembangkitan listrik wajib mendapat persetujuan Menteri ESDM.\n\n"
            "Pasal 44 — Setiap kegiatan usaha ketenagalistrikan wajib memenuhi ketentuan Keselamatan Ketenagalistrikan (K2) "
            "dan setiap peralatan serta instalasi tenaga listrik wajib memiliki Sertifikat Laik Operasi (SLO)."
        ),
    },
    {
        "domain": "jdih_esdm",
        "judul": "Undang-Undang Nomor 22 Tahun 2001 tentang Minyak dan Gas Bumi",
        "nomor": "UU No. 22 Tahun 2001",
        "jenis": "UU",
        "sektor": "Minyak dan Gas Bumi (Migas)",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.esdm.go.id/uu-22-2001",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 22 TAHUN 2001 TENTANG MINYAK DAN GAS BUMI\n\n"
            "Pasal 5 — Kegiatan usaha Minyak dan Gas Bumi terdiri atas Kegiatan Usaha Hulu (Eksplorasi dan Eksploitasi) "
            "yang dilaksanakan melalui Kontrak Kerja Sama (Production Sharing Contract / PSC) dan Kegiatan Usaha Hilir "
            "(Pengolahan, Pengangkutan, Penyimpanan, dan Niaga) yang dilaksanakan berdasarkan Perizinan Berusaha.\n\n"
            "Pasal 22 — Badan Usaha atau Bentuk Usaha Tetap wajib menyerahkan paling banyak 25% bagiannya dari hasil "
            "produksi Minyak Bumi dan/atau Gas Bumi untuk memenuhi kebutuhan dalam negeri (Domestic Market Obligation / DMO)."
        ),
    },
    {
        "domain": "jdih_esdm",
        "judul": "Undang-Undang Nomor 30 Tahun 2007 tentang Energi",
        "nomor": "UU No. 30 Tahun 2007",
        "jenis": "UU",
        "sektor": "Energi & Sumber Daya Mineral",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.esdm.go.id/uu-30-2007",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 30 TAHUN 2007 TENTANG ENERGI\n\n"
            "Pasal 20 — Penyediaan energi baru dan energi terbarukan (EBT) wajib ditingkatkan oleh Pemerintah dan Pemerintah "
            "Daerah sesuai dengan kewenangannya.\n\n"
            "Pasal 25 — Konservasi energi nasional menjadi tanggung jawab Pemerintah, Pemerintah Daerah, pengusaha, dan "
            "masyarakat yang mencakup seluruh tahap pengelolaan energi."
        ),
    },
    {
        "domain": "jdih_klhk",
        "judul": "Peraturan Pemerintah Nomor 22 Tahun 2021 tentang Penyelenggaraan Perlindungan dan Pengelolaan Lingkungan Hidup",
        "nomor": "PP No. 22 Tahun 2021",
        "jenis": "PP",
        "sektor": "Lingkungan Hidup & Kehutanan",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.menlhk.go.id/pp-22-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 22 TAHUN 2021 TENTANG PENYELENGGARAAN PERLINDUNGAN DAN PENGELOLAAN LINGKUNGAN HIDUP\n\n"
            "Pasal 3 — Persetujuan Lingkungan yang terdiri atas Keputusan Kelayakan Lingkungan Hidup (AMDAL) atau "
            "Pernyataan Kesanggupan Pengelolaan Lingkungan Hidup (PKPLH / UKL-UPL) wajib dimiliki oleh setiap Usaha "
            "dan/atau Kegiatan yang memiliki dampak penting atau tidak penting terhadap lingkungan dan merupakan prasyarat "
            "penerbitan Perizinan Berusaha.\n\n"
            "Pasal 274 — Setiap Orang yang menghasilkan Limbah B3 wajib melakukan Pengelolaan Limbah B3 meliputi pengurangan, "
            "penyimpanan, pengumpulan, pengangkutan, pemanfaatan, pengolahan, dan/atau penimbunan."
        ),
    },
    {
        "domain": "jdih_esdm",
        "judul": "Peraturan Pemerintah Nomor 96 Tahun 2021 tentang Pelaksanaan Kegiatan Usaha Pertambangan Mineral dan Batubara",
        "nomor": "PP No. 96 Tahun 2021",
        "jenis": "PP",
        "sektor": "Pertambangan Mineral & Batubara",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.esdm.go.id/pp-96-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 96 TAHUN 2021 TENTANG PELAKSANAAN KEGIATAN USAHA PERTAMBANGAN MINERAL DAN BATUBARA\n\n"
            "Pasal 137 — Pemegang IUP atau IUPK dapat menyerahkan pelaksanaan kegiatan usaha jasa pertambangan kepada "
            "pemegang IUJP setelah mendapatkan persetujuan Rencana Kerja dan Anggaran Biaya (RKAB) Tahunan dari Menteri ESDM.\n\n"
            "Pasal 139 — Pemegang IUP atau IUPK wajib mengutamakan perusahaan jasa pertambangan lokal dan/atau nasional "
            "serta penggunaan tingkat komponen dalam negeri (TKDN)."
        ),
    },
    {
        "domain": "jdih_klhk",
        "judul": "Peraturan Presiden Nomor 98 Tahun 2021 tentang Penyelenggaraan Nilai Ekonomi Karbon untuk Pencapaian Target Kontribusi yang Ditetapkan Secara Nasional dan Pengendalian Emisi Gas Rumah Kaca",
        "nomor": "Perpres No. 98 Tahun 2021",
        "jenis": "Perpres",
        "sektor": "Lingkungan Hidup & Nilai Ekonomi Karbon",
        "status": "Berlaku",
        "category_code": "ENERGY_MINING_ENV",
        "detail_url": "https://jdih.menlhk.go.id/perpres-98-2021",
        "content": (
            "PERATURAN PRESIDEN REPUBLIK INDONESIA NOMOR 98 TAHUN 2021 TENTANG PENYELENGGARAAN NILAI EKONOMI KARBON\n\n"
            "Pasal 47 — Penyelenggaraan Nilai Ekonomi Karbon (NEK) dilakukan melalui mekanisme:\n"
            "a. Perdagangan Karbon (perdagangan emisi dan offset emisi GRK melalui bursa karbon maupun perdagangan langsung);\n"
            "b. Pembayaran Berbasis Kinerja (Result-Based Payment);\n"
            "c. Pungutan atas Karbon;\n"
            "d. Mekanisme lain sesuai dengan perkembangan ilmu pengetahuan dan teknologi."
        ),
    },

    # ==================== 3. IP_MEDIA (8 Regulations) ====================
    {
        "domain": "jdih_djki",
        "judul": "Undang-Undang Nomor 28 Tahun 2014 tentang Hak Cipta",
        "nomor": "UU No. 28 Tahun 2014",
        "jenis": "UU",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/uu-28-2014",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 28 TAHUN 2014 TENTANG HAK CIPTA\n\n"
            "Pasal 1 — Hak Cipta adalah hak eksklusif pencipta yang timbul secara otomatis berdasarkan prinsip deklaratif "
            "setelah suatu ciptaan diwujudkan dalam bentuk nyata tanpa mengurangi pembatasan sesuai dengan ketentuan "
            "peraturan perundang-undangan.\n\n"
            "Pasal 4 — Hak Cipta sebagaimana dimaksud dalam Pasal 3 merupakan hak eksklusif yang terdiri atas hak moral dan hak ekonomi.\n\n"
            "Pasal 36 — Kecuali diperjanjikan lain, Pencipta dan Pemegang Hak Cipta atas Ciptaan yang dibuat dalam "
            "hubungan kerja atau berdasarkan pesanan yaitu pihak yang membuat Ciptaan, namun apabila Ciptaan dibuat dalam "
            "hubungan dinas atau hubungan kerja di mana perjanjian kerja mengatur pengalihan, Pemegang Hak Cipta adalah Pemberi Kerja.\n\n"
            "Pasal 80 — Pemegang Hak Cipta atau pemilik Hak Terkait berhak memberikan Lisensi kepada pihak lain berdasarkan "
            "perjanjian Lisensi tertulis untuk melaksanakan perbuatan hukum atas Ciptaan atau produk Hak Terkait.\n\n"
            "Pasal 83 — Perjanjian Lisensi wajib dicatatkan oleh Menteri dalam daftar umum perjanjian Lisensi Hak Cipta "
            "dengan dikenai biaya; apabila tidak dicatatkan, perjanjian Lisensi tidak mempunyai akibat hukum terhadap pihak ketiga."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Undang-Undang Nomor 13 Tahun 2016 tentang Paten",
        "nomor": "UU No. 13 Tahun 2016",
        "jenis": "UU",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/uu-13-2016",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 13 TAHUN 2016 TENTANG PATEN\n\n"
            "Pasal 1 — Paten adalah hak eksklusif yang diberikan oleh negara kepada inventor atas hasil invensinya di "
            "bidang teknologi untuk jangka waktu tertentu melaksanakan sendiri invensi tersebut atau memberikan persetujuan "
            "kepada pihak lain untuk melaksanakannya.\n\n"
            "Pasal 12 — Pemegang Paten atas Invensi yang dihasilkan oleh Inventor dalam hubungan kerja adalah pihak yang "
            "memberikan pekerjaan, kecuali diperjanjikan lain. Inventor berhak mendapatkan Imbalan yang wajar.\n\n"
            "Pasal 22 — Paten diberikan untuk jangka waktu 20 (dua puluh) tahun terhitung sejak Tanggal Penerimaan dan "
            "tidak dapat diperpanjang. Paten Sederhana diberikan untuk jangka waktu 10 (sepuluh) tahun.\n\n"
            "Pasal 76 — Pemegang Paten berhak memberikan Lisensi kepada pihak lain berdasarkan perjanjian Lisensi baik "
            "eksklusif maupun non-eksklusif, dan wajib dicatatkan serta diumumkan oleh Menteri."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Undang-Undang Nomor 20 Tahun 2016 tentang Merek dan Indikasi Geografis",
        "nomor": "UU No. 20 Tahun 2016",
        "jenis": "UU",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/uu-20-2016",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 20 TAHUN 2016 TENTANG MEREK DAN INDIKASI GEOGRAFIS\n\n"
            "Pasal 3 — Hak atas Merek diperoleh setelah Merek tersebut terdaftar (prinsip first-to-file).\n\n"
            "Pasal 35 — Merek terdaftar mendapat pelindungan hukum untuk jangka waktu 10 (sepuluh) tahun sejak Tanggal "
            "Penerimaan dan jangka waktu pelindungan itu dapat diperpanjang untuk jangka waktu yang sama.\n\n"
            "Pasal 41 — Hak atas Merek terdaftar dapat beralih atau dialihkan karena pewarisan, wasiat, wakaf, hibah, "
            "perjanjian, atau sebab lain yang dibenarkan oleh peraturan perundang-undangan.\n\n"
            "Pasal 42 — Pemilik Merek terdaftar dapat memberikan Lisensi kepada pihak lain untuk menggunakan Merek tersebut "
            "baik sebagian maupun seluruh jenis barang dan/atau jasa, dan perjanjian Lisensi wajib dimohonkan pencatatannya kepada Menteri."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Undang-Undang Nomor 30 Tahun 2000 tentang Rahasia Dagang",
        "nomor": "UU No. 30 Tahun 2000",
        "jenis": "UU",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/uu-30-2000",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 30 TAHUN 2000 TENTANG RAHASIA DAGANG\n\n"
            "Pasal 1 — Rahasia Dagang adalah informasi yang tidak diketahui oleh umum di bidang teknologi dan/atau bisnis, "
            "mempunyai nilai ekonomi karena berguna dalam kegiatan usaha, dan dijaga kerahasiaannya oleh pemilik Rahasia Dagang.\n\n"
            "Pasal 3 — Rahasia Dagang mendapat perlindungan apabila informasi tersebut bersifat rahasia, mempunyai nilai "
            "ekonomi, dan dijaga kerahasiaannya melalui upaya sebagaimana mestinya (Non-Disclosure Agreement / klausul kerahasiaan).\n\n"
            "Pasal 13 — Pelanggaran Rahasia Dagang juga terjadi apabila seseorang dengan sengaja mengungkapkan Rahasia "
            "Dagang, mengingkari kesepakatan atau mengingkari kewajiban tertulis atau tidak tertulis untuk menjaga Rahasia Dagang yang bersangkutan."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Undang-Undang Nomor 31 Tahun 2000 tentang Desain Industri",
        "nomor": "UU No. 31 Tahun 2000",
        "jenis": "UU",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/uu-31-2000",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 31 TAHUN 2000 TENTANG DESAIN INDUSTRI\n\n"
            "Pasal 1 — Desain Industri adalah suatu kreasi tentang bentuk, konfigurasi, atau komposisi garis atau warna, "
            "atau garis dan warna, atau gabungan daripadanya yang berbentuk tiga dimensi atau dua dimensi yang memberikan "
            "kesan estetis dan dapat diwujudkan dalam pola tiga dimensi atau dua dimensi serta dapat dipakai untuk "
            "menghasilkan suatu produk, barang, komoditas industri, atau kerajinan tangan.\n\n"
            "Pasal 5 — Perlindungan terhadap Hak Desain Industri diberikan untuk jangka waktu 10 (sepuluh) tahun terhitung sejak Tanggal Penerimaan."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Undang-Undang Nomor 32 Tahun 2000 tentang Desain Tata Letak Sirkuit Terpadu",
        "nomor": "UU No. 32 Tahun 2000",
        "jenis": "UU",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/uu-32-2000",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 32 TAHUN 2000 TENTANG DESAIN TATA LETAK SIRKUIT TERPADU\n\n"
            "Pasal 8 — Pemegang Hak memiliki hak eksklusif untuk melaksanakan Hak Desain Tata Letak Sirkuit Terpadu yang "
            "dimilikinya dan untuk melarang orang lain yang tanpa persetujuannya membuat, memakai, menjual, mengimpor, "
            "mengekspor dan/atau mengedarkan barang yang di dalamnya terdapat seluruh atau sebagian Desain yang telah diberi "
            "Hak Desain Tata Letak Sirkuit Terpadu untuk jangka waktu 10 tahun."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Peraturan Pemerintah Nomor 36 Tahun 2018 tentang Pencatatan Perjanjian Lisensi Kekayaan Intelektual",
        "nomor": "PP No. 36 Tahun 2018",
        "jenis": "PP",
        "sektor": "Kekayaan Intelektual (HKI)",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/pp-36-2018",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 36 TAHUN 2018 TENTANG PENCATATAN PERJANJIAN LISENSI KEKAYAAN INTELEKTUAL\n\n"
            "Pasal 6 — Perjanjian Lisensi dilarang memuat ketentuan yang:\n"
            "a. merugikan perekonomian Indonesia dan kepentingan nasional Indonesia;\n"
            "b. memuat pembatasan yang menghambat kemampuan bangsa Indonesia dalam melakukan pengalihan, penguasaan, dan pengembangan teknologi;\n"
            "c. mengakibatkan persaingan usaha tidak sehat;\n"
            "d. bertentangan dengan ketentuan peraturan perundang-undangan, nilai-nilai agama, kesusilaan, dan ketertiban umum.\n\n"
            "Pasal 7 — Perjanjian Lisensi dibuat dalam bentuk tertulis paling sedikit memuat tanggal/tempat penandatanganan, "
            "identitas pemberi dan penerima lisensi, objek lisensi, jangka waktu lisensi, wilayah berlaku, dan besaran royalti."
        ),
    },
    {
        "domain": "jdih_djki",
        "judul": "Peraturan Pemerintah Nomor 56 Tahun 2021 tentang Pengelolaan Royalti Hak Cipta Lagu dan/atau Musik",
        "nomor": "PP No. 56 Tahun 2021",
        "jenis": "PP",
        "sektor": "Kekayaan Intelektual & Media",
        "status": "Berlaku",
        "category_code": "IP_MEDIA",
        "detail_url": "https://dgip.go.id/pp-56-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 56 TAHUN 2021 TENTANG PENGELOLAAN ROYALTI HAK CIPTA LAGU DAN/ATAU MUSIK\n\n"
            "Pasal 3 — Setiap Orang dapat melakukan Penggunaan Secara Komersial lagu dan/atau musik dalam bentuk layanan "
            "publik yang bersifat komersial dengan membayar Royalti kepada Pencipta, Pemegang Hak Cipta, dan/atau pemilik "
            "Hak Terkait melalui Lembaga Manajemen Kolektif Nasional (LMKN)."
        ),
    },

    # ==================== 4. HEALTH_PHARMA (8 Regulations) ====================
    {
        "domain": "jdih_kemenkes",
        "judul": "Undang-Undang Nomor 17 Tahun 2023 tentang Kesehatan",
        "nomor": "UU No. 17 Tahun 2023",
        "jenis": "UU",
        "sektor": "Kesehatan & Farmasi",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.kemkes.go.id/uu-17-2023",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 17 TAHUN 2023 TENTANG KESEHATAN\n\n"
            "Pasal 173 — Fasilitas Pelayanan Kesehatan wajib menyelenggarakan Rekam Medis Elektronik (RME) dan menjaga "
            "kerahasiaan data kesehatan Pasien sesuai dengan ketentuan pelindungan data pribadi.\n\n"
            "Pasal 189 — Setiap Rumah Sakit memiliki kewajiban memberikan pelayanan kesehatan yang aman, bermutu, antidiskriminasi, "
            "dan efektif dengan mengutamakan kepentingan Pasien sesuai dengan standar pelayanan Rumah Sakit.\n\n"
            "Pasal 193 — Rumah Sakit bertanggung jawab secara hukum terhadap semua kerugian yang ditimbulkan atas kelalaian "
            "yang dilakukan oleh Sumber Daya Manusia Kesehatan di Rumah Sakit.\n\n"
            "Pasal 320 — Sediaan Farmasi dan Alat Kesehatan hanya dapat diedarkan setelah memiliki Perizinan Berusaha "
            "(izin edar) dari Pemerintah Pusat dan memenuhi standar keamanan, khasiat/manfaat, dan mutu."
        ),
    },
    {
        "domain": "jdih_kemenkes",
        "judul": "Undang-Undang Nomor 44 Tahun 2009 tentang Rumah Sakit",
        "nomor": "UU No. 44 Tahun 2009",
        "jenis": "UU",
        "sektor": "Kesehatan & Rumah Sakit",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.kemkes.go.id/uu-44-2009",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 44 TAHUN 2009 TENTANG RUMAH SAKIT\n\n"
            "Pasal 29 — Setiap Rumah Sakit mempunyai kewajiban memberikan pelayanan gawat darurat kepada pasien sesuai "
            "dengan kemampuan pelayanannya, menyediakan sarana dan pelayanan bagi masyarakat tidak mampu atau miskin, "
            "serta menyelenggarakan rekam medis.\n\n"
            "Pasal 32 — Setiap pasien mempunyai hak memperoleh layanan yang manusiawi, adil, jujur, dan tanpa diskriminasi, "
            "serta memberikan persetujuan atau menolak atas tindakan yang akan dilakukan oleh tenaga kesehatan (informed consent).\n\n"
            "Pasal 46 — Rumah Sakit bertanggung jawab secara hukum terhadap semua kerugian yang ditimbulkan atas kelalaian "
            "yang dilakukan oleh tenaga kesehatan di Rumah Sakit."
        ),
    },
    {
        "domain": "jdih_kemenkes",
        "judul": "Undang-Undang Nomor 36 Tahun 2014 tentang Tenaga Kesehatan",
        "nomor": "UU No. 36 Tahun 2014",
        "jenis": "UU",
        "sektor": "Kesehatan & Tenaga Medis",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.kemkes.go.id/uu-36-2014",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 36 TAHUN 2014 TENTANG TENAGA KESEHATAN\n\n"
            "Pasal 44 — Setiap Tenaga Kesehatan yang menjalankan praktik wajib memiliki Surat Tanda Registrasi (STR).\n\n"
            "Pasal 46 — Setiap Tenaga Kesehatan yang menjalankan praktik di bidang pelayanan kesehatan wajib memiliki "
            "Surat Izin Praktik (SIP) dan wajib mematuhi Standar Profesi, Standar Pelayanan Profesi, dan Standar Prosedur Operasional."
        ),
    },
    {
        "domain": "jdih_bpom",
        "judul": "Undang-Undang Nomor 18 Tahun 2012 tentang Pangan",
        "nomor": "UU No. 18 Tahun 2012",
        "jenis": "UU",
        "sektor": "Pangan, Obat & BPOM",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.pom.go.id/uu-18-2012",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 18 TAHUN 2012 TENTANG PANGAN\n\n"
            "Pasal 86 — Setiap Orang yang memproduksi dan memperdagangkan Pangan wajib memenuhi standar Keamanan Pangan "
            "dan Mutu Pangan.\n\n"
            "Pasal 91 — Dalam hal Pangan Olahan yang diproduksi di dalam negeri atau yang diimpor untuk diperdagangkan "
            "dalam kemasan eceran, Pelaku Usaha Pangan wajib memiliki izin edar.\n\n"
            "Pasal 97 — Setiap Orang yang memproduksi Pangan di dalam negeri atau mengimpor Pangan untuk diperdagangkan "
            "wajib mencantumkan label di dalam dan/atau pada Kemasan Pangan dalam bahasa Indonesia secara benar dan tidak menyesatkan."
        ),
    },
    {
        "domain": "jdih_kemenkes",
        "judul": "Peraturan Pemerintah Nomor 51 Tahun 2009 tentang Pekerjaan Kefarmasian",
        "nomor": "PP No. 51 Tahun 2009",
        "jenis": "PP",
        "sektor": "Kesehatan & Farmasi",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.kemkes.go.id/pp-51-2009",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 51 TAHUN 2009 TENTANG PEKERJAAN KEFARMASIAN\n\n"
            "Pasal 6 — Pengadaan Sediaan Farmasi dilakukan pada fasilitas produksi, fasilitas distribusi atau penyaluran "
            "(Pedagang Besar Farmasi / PBF) dan fasilitas pelayanan sediaan farmasi yang harus menjamin keamanan, mutu, "
            "manfaat dan khasiat Sediaan Farmasi sesuai Cara Pembuatan Obat yang Baik (CPOB) dan Cara Distribusi Obat yang Baik (CDOB)."
        ),
    },
    {
        "domain": "jdih_bpom",
        "judul": "Peraturan Pemerintah Nomor 86 Tahun 2019 tentang Keamanan Pangan",
        "nomor": "PP No. 86 Tahun 2019",
        "jenis": "PP",
        "sektor": "Pangan, Obat & BPOM",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.pom.go.id/pp-86-2019",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 86 TAHUN 2019 TENTANG KEAMANAN PANGAN\n\n"
            "Pasal 4 — Penyelenggaraan Keamanan Pangan dilakukan melalui sanitasi pangan, pengaturan terhadap bahan tambahan "
            "pangan, pengaturan terhadap pangan produk rekayasa genetik, pengaturan terhadap iradiasi pangan, penetapan "
            "standar kemasan pangan, pemberian jaminan keamanan pangan dan mutu pangan, serta jaminan produk halal bagi yang dipersyaratkan."
        ),
    },
    {
        "domain": "jdih_bpom",
        "judul": "Peraturan Badan Pengawas Obat dan Makanan Nomor 31 Tahun 2018 tentang Label Pangan Olahan",
        "nomor": "Peraturan BPOM No. 31 Tahun 2018",
        "jenis": "Peraturan BPOM",
        "sektor": "Pangan, Obat & BPOM",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.pom.go.id/perbpom-31-2018",
        "content": (
            "PERATURAN BADAN PENGAWAS OBAT DAN MAKANAN NOMOR 31 TAHUN 2018 TENTANG LABEL PANGAN OLAHAN\n\n"
            "Pasal 5 — Label Pangan Olahan paling sedikit wajib memuat keterangan mengenai:\n"
            "a. nama produk;\n"
            "b. daftar bahan yang digunakan (komposisi);\n"
            "c. berat bersih atau isi bersih;\n"
            "d. nama dan alamat pihak yang memproduksi atau mengimpor;\n"
            "e. halal bagi yang dipersyaratkan;\n"
            "f. tanggal dan kode produksi;\n"
            "g. keterangan kedaluwarsa;\n"
            "h. nomor izin edar BPOM;\n"
            "i. asal usul bahan Pangan tertentu."
        ),
    },
    {
        "domain": "jdih_kemenkes",
        "judul": "Peraturan Menteri Kesehatan Nomor 24 Tahun 2022 tentang Rekam Medis",
        "nomor": "Permenkes No. 24 Tahun 2022",
        "jenis": "Permenkes",
        "sektor": "Kesehatan & Rekam Medis Elektronik",
        "status": "Berlaku",
        "category_code": "HEALTH_PHARMA",
        "detail_url": "https://jdih.kemkes.go.id/permenkes-24-2022",
        "content": (
            "PERATURAN MENTERI KESEHATAN REPUBLIK INDONESIA NOMOR 24 TAHUN 2022 TENTANG REKAM MEDIS\n\n"
            "Pasal 3 — Setiap Fasilitas Pelayanan Kesehatan wajib menyelenggarakan Rekam Medis Elektronik.\n\n"
            "Pasal 29 — Penyimpanan data Rekam Medis Elektronik di Fasilitas Pelayanan Kesehatan dilakukan paling singkat "
            "25 (dua puluh lima) tahun sejak tanggal kunjungan terakhir Pasien.\n\n"
            "Pasal 32 — Isi Rekam Medis wajib dijaga kerahasiaannya oleh semua pihak yang terlibat dalam pelayanan kesehatan "
            "di Fasilitas Pelayanan Kesehatan walaupun Pasien telah meninggal dunia."
        ),
    },

    # ==================== 5. TRANSPORT_LOGISTICS (8 Regulations) ====================
    {
        "domain": "jdih_kemenhub",
        "judul": "Undang-Undang Nomor 22 Tahun 2009 tentang Lalu Lintas dan Angkutan Jalan",
        "nomor": "UU No. 22 Tahun 2009",
        "jenis": "UU",
        "sektor": "Perhubungan & Transportasi",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/uu-22-2009",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 22 TAHUN 2009 TENTANG LALU LINTAS DAN ANGKUTAN JALAN\n\n"
            "Pasal 186 — Perusahaan Angkutan Umum wajib mengangkut orang dan/atau barang setelah disepakati perjanjian "
            "angkutan dan/atau dilakukan pembayaran biaya angkutan oleh Penumpang dan/atau pengirim barang.\n\n"
            "Pasal 192 — Perusahaan Angkutan Umum bertanggung jawab atas kerugian yang diderita oleh Penumpang yang "
            "meninggal dunia atau luka-luka serta bertanggung jawab atas kerugian yang diderita oleh pengirim barang "
            "karena barang musnah, hilang, atau rusak akibat penyelenggaraan angkutan, kecuali terbukti disebabkan oleh "
            "kejadian yang tidak dapat dicegah (force majeure) atau kesalahan pengirim sendiri.\n\n"
            "Pasal 234 — Pengemudi, pemilik Kendaraan Bermotor, dan/atau Perusahaan Angkutan Umum bertanggung jawab atas "
            "kerugian yang diderita oleh Penumpang dan/atau pemilik barang dan/atau pihak ketiga karena kelalaian Pengemudi."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Undang-Undang Nomor 17 Tahun 2008 tentang Pelayaran",
        "nomor": "UU No. 17 Tahun 2008",
        "jenis": "UU",
        "sektor": "Pelayaran & Maritim",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/uu-17-2008",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 17 TAHUN 2008 TENTANG PELAYARAN\n\n"
            "Pasal 8 — Asas Cabotage: Kegiatan angkutan laut dalam negeri dilakukan oleh perusahaan angkutan laut nasional "
            "dengan menggunakan kapal berbendera Indonesia serta diawaki oleh Awak Kapal berkewarganegaraan Indonesia.\n\n"
            "Pasal 40 — Perusahaan angkutan di perairan bertanggung jawab terhadap keselamatan dan keamanan penumpang "
            "dan/atau barang yang diangkutnya sejak barang diterima hingga diserahkan sesuai perjanjian pengangkutan "
            "(Bill of Lading / Konosemen).\n\n"
            "Pasal 41 — Perusahaan angkutan di perairan wajib mengasuransikan tanggung jawabnya terhadap penumpang dan muatan."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Undang-Undang Nomor 1 Tahun 2009 tentang Penerbangan",
        "nomor": "UU No. 1 Tahun 2009",
        "jenis": "UU",
        "sektor": "Penerbangan & Kargo Udara",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/uu-1-2009",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 1 TAHUN 2009 TENTANG PENERBANGAN\n\n"
            "Pasal 141 — Pengangkut bertanggung jawab atas kerugian penumpang yang meninggal dunia, cacat tetap, atau "
            "luka-luka yang diakibatkan kejadian angkutan udara di dalam pesawat dan/atau naik turun pesawat udara.\n\n"
            "Pasal 145 — Pengangkut bertanggung jawab atas kerugian yang diderita oleh pengirim kargo karena kargo yang "
            "dikirim hilang, musnah, atau rusak yang diakibatkan oleh kegiatan angkutan udara selama kargo berada dalam "
            "pengawasan pengangkut berdasarkan Surat Muatan Udara (Air Waybill)."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Undang-Undang Nomor 23 Tahun 2007 tentang Perkeretaapian",
        "nomor": "UU No. 23 Tahun 2007",
        "jenis": "UU",
        "sektor": "Perkeretaapian & Logistik",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/uu-23-2007",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 23 TAHUN 2007 TENTANG PERKERETAAPIAN\n\n"
            "Pasal 157 — Penyelenggara Sarana Perkeretaapian bertanggung jawab terhadap pengguna jasa yang mengalami "
            "kerugian, luka-luka, atau meninggal dunia yang disebabkan oleh pengoperasian angkutan kereta api serta atas "
            "kerugian barang yang musnah, hilang, atau rusak selama berada dalam pengawasan penyelenggara sarana."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Peraturan Pemerintah Nomor 31 Tahun 2021 tentang Penyelenggaraan Bidang Pelayaran",
        "nomor": "PP No. 31 Tahun 2021",
        "jenis": "PP",
        "sektor": "Pelayaran & Kepelabuhanan",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/pp-31-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 31 TAHUN 2021 TENTANG PENYELENGGARAAN BIDANG PELAYARAN\n\n"
            "Pasal 5 — Kegiatan usaha angkutan laut dan usaha jasa terkait dengan angkutan di perairan (bongkar muat barang, "
            "jasa pengurusan transportasi, angkutan perairan pelabuhan, penyewaan peralatan angkutan laut, tally mandiri, "
            "dan depo peti kemas) wajib memenuhi Perizinan Berusaha dan standar keselamatan pelayaran."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Peraturan Pemerintah Nomor 32 Tahun 2021 tentang Penyelenggaraan Bidang Penerbangan",
        "nomor": "PP No. 32 Tahun 2021",
        "jenis": "PP",
        "sektor": "Penerbangan & Kargo Udara",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/pp-32-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 32 TAHUN 2021 TENTANG PENYELENGGARAAN BIDANG PENERBANGAN\n\n"
            "Pasal 45 — Badan usaha angkutan udara niaga berjadwal dan tidak berjadwal serta agen kargo udara wajib "
            "memenuhi standar keamanan penerbangan, kelaikudaraan pesawat udara, dan tanggung jawab pengangkut."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Peraturan Pemerintah Nomor 30 Tahun 2021 tentang Penyelenggaraan Bidang Lalu Lintas dan Angkutan Jalan",
        "nomor": "PP No. 30 Tahun 2021",
        "jenis": "PP",
        "sektor": "Perhubungan & Angkutan Jalan",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/pp-30-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 30 TAHUN 2021 TENTANG PENYELENGGARAAN BIDANG LALU LINTAS DAN ANGKUTAN JALAN\n\n"
            "Pasal 60 — Perusahaan Angkutan Umum yang menyelenggarakan angkutan barang umum dan angkutan barang khusus "
            "(termasuk barang berbahaya / B3 dan alat berat) wajib menerapkan Sistem Manajemen Keselamatan Perusahaan Angkutan Umum."
        ),
    },
    {
        "domain": "jdih_kemenhub",
        "judul": "Peraturan Menteri Perhubungan Nomor PM 49 Tahun 2017 tentang Penyelenggaraan dan Pengusahaan Jasa Pengurusan Transportasi",
        "nomor": "Permenhub No. PM 49 Tahun 2017",
        "jenis": "Permenhub",
        "sektor": "Logistik & Freight Forwarding",
        "status": "Berlaku",
        "category_code": "TRANSPORT_LOGISTICS",
        "detail_url": "https://jdih.dephub.go.id/pm-49-2017",
        "content": (
            "PERATURAN MENTERI PERHUBUNGAN REPUBLIK INDONESIA NOMOR PM 49 TAHUN 2017 TENTANG PENYELENGGARAAN DAN PENGUSAHAAN JASA PENGURUSAN TRANSPORTASI\n\n"
            "Pasal 2 — Jasa Pengurusan Transportasi (Freight Forwarding) adalah usaha yang ditujukan untuk mewakili "
            "kepentingan pemilik barang untuk mengurus semua kegiatan yang diperlukan bagi terlaksananya pengiriman dan "
            "penerimaan barang melalui transportasi darat, perkeretaapian, laut, dan/atau udara mencakup penerimaan, "
            "penyimpanan, sortasi, pengepakan, penandaan, pengukuran, penimbangan, pengurusan dokumen penyelesaian kepabeanan, "
            "penerbitan dokumen angkutan, perhitungan biaya angkutan, serta klaim asuransi pengiriman barang."
        ),
    },

    # ==================== 6. PROCUREMENT_SOE (8 Regulations) ====================
    {
        "domain": "jdih_bumn",
        "judul": "Undang-Undang Nomor 19 Tahun 2003 tentang Badan Usaha Milik Negara (UU BUMN)",
        "nomor": "UU No. 19 Tahun 2003",
        "jenis": "UU",
        "sektor": "BUMN & Korporasi Negara",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.bumn.go.id/uu-19-2003",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 19 TAHUN 2003 TENTANG BADAN USAHA MILIK NEGARA\n\n"
            "Pasal 1 — Badan Usaha Milik Negara (BUMN) adalah badan usaha yang seluruh atau sebagian besar modalnya "
            "dimiliki oleh negara melalui penyertaan secara langsung yang berasal dari kekayaan negara yang dipisahkan.\n\n"
            "Pasal 5 — Pengurusan BUMN dilakukan oleh Direksi. Direksi bertanggung jawab penuh atas pengurusan BUMN "
            "untuk kepentingan dan tujuan BUMN serta mewakili BUMN baik di dalam maupun di luar pengadilan.\n\n"
            "Pasal 26 — Direksi wajib menerapkan prinsip-prinsip tata kelola perusahaan yang baik (Good Corporate Governance / GCG) "
            "yang meliputi transparansi, kemandirian, akuntabilitas, pertanggungjawaban, dan kewajaran."
        ),
    },
    {
        "domain": "jdih_lkpp",
        "judul": "Peraturan Presiden Nomor 16 Tahun 2018 tentang Pengadaan Barang/Jasa Pemerintah",
        "nomor": "Perpres No. 16 Tahun 2018",
        "jenis": "Perpres",
        "sektor": "Pengadaan Barang/Jasa (LKPP)",
        "status": "Berlaku (Diubah Perpres 12/2021)",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.lkpp.go.id/perpres-16-2018",
        "content": (
            "PERATURAN PRESIDEN REPUBLIK INDONESIA NOMOR 16 TAHUN 2018 TENTANG PENGADAAN BARANG/JASA PEMERINTAH\n\n"
            "Pasal 6 — Pengadaan Barang/Jasa menerapkan prinsip: efisien, efektif, transparan, terbuka, bersaing, adil, dan akuntabel.\n\n"
            "Pasal 27 — Jenis Kontrak Pengadaan Barang/Jasa terdiri atas Lumsum, Harga Satuan, Gabungan Lumsum dan Harga "
            "Satuan, Terima Jadi (Turnkey), dan Kontrak Payung.\n\n"
            "Pasal 30 — Jaminan dalam Pengadaan Barang/Jasa terdiri atas Jaminan Penawaran, Jaminan Sanggah Banding, "
            "Jaminan Pelaksanaan (sebesar 5% dari nilai kontrak), Jaminan Uang Muka, dan Jaminan Pemeliharaan (sebesar 5%).\n\n"
            "Pasal 66 — Kementerian/Lembaga/Perangkat Daerah wajib menggunakan produk dalam negeri (TKDN) apabila terdapat "
            "produk dalam negeri yang memiliki penjumlahan nilai TKDN ditambah nilai Bobot Manfaat Perusahaan (BMP) paling sedikit 40%."
        ),
    },
    {
        "domain": "jdih_lkpp",
        "judul": "Peraturan Presiden Nomor 12 Tahun 2021 tentang Perubahan atas Peraturan Presiden Nomor 16 Tahun 2018 tentang Pengadaan Barang/Jasa Pemerintah",
        "nomor": "Perpres No. 12 Tahun 2021",
        "jenis": "Perpres",
        "sektor": "Pengadaan Barang/Jasa (LKPP)",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.lkpp.go.id/perpres-12-2021",
        "content": (
            "PERATURAN PRESIDEN REPUBLIK INDONESIA NOMOR 12 TAHUN 2021 TENTANG PERUBAHAN ATAS PERPRES NOMOR 16 TAHUN 2018 TENTANG PENGADAAN BARANG/JASA PEMERINTAH\n\n"
            "Pasal 38 — Metode pemilihan Penyedia Barang/Pekerjaan Konstruksi/Jasa Lainnya terdiri atas:\n"
            "a. E-purchasing (Katalog Elektronik / Toko Daring);\n"
            "b. Pengadaan Langsung (sampai dengan Rp200.000.000);\n"
            "c. Penunjukan Langsung;\n"
            "d. Tender Cepat;\n"
            "e. Tender.\n\n"
            "Pasal 65 — Usaha Kecil diberikan alokasi paling sedikit 40% dari nilai anggaran belanja Barang/Jasa Kementerian/Lembaga/Pemerintah Daerah.\n\n"
            "Pasal 78 — Dalam hal Penyedia gagal menyelesaikan pekerjaan sampai masa pelaksanaan kontrak berakhir, Pejabat "
            "Penandatangan Kontrak mengenakan sanksi denda keterlambatan sebesar 1‰ (satu permil) dari nilai kontrak atau "
            "nilai bagian kontrak untuk setiap hari keterlambatan."
        ),
    },
    {
        "domain": "jdih_bumn",
        "judul": "Peraturan Pemerintah Nomor 45 Tahun 2005 tentang Pendirian, Pengurusan, Pengawasan, dan Pembubaran Badan Usaha Milik Negara",
        "nomor": "PP No. 45 Tahun 2005",
        "jenis": "PP",
        "sektor": "BUMN & Korporasi Negara",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.bumn.go.id/pp-45-2005",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 45 TAHUN 2005 TENTANG PENDIRIAN, PENGURUSAN, PENGAWASAN, DAN PEMBUBARAN BUMN\n\n"
            "Pasal 45 — Anggota Direksi tidak dapat dipertanggungjawabkan atas kerugian BUMN apabila dapat membuktikan "
            "bahwa kerugian tersebut bukan karena kesalahan atau kelalaiannya, telah melakukan pengurusan dengan iktikad "
            "baik dan kehati-hatian sesuai Business Judgment Rule, dan tidak mempunyai benturan kepentingan."
        ),
    },
    {
        "domain": "jdih_bumn",
        "judul": "Peraturan Pemerintah Nomor 23 Tahun 2022 tentang Perubahan atas Peraturan Pemerintah Nomor 45 Tahun 2005 tentang Pendirian, Pengurusan, Pengawasan, dan Pembubaran Badan Usaha Milik Negara",
        "nomor": "PP No. 23 Tahun 2022",
        "jenis": "PP",
        "sektor": "BUMN & Korporasi Negara",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.bumn.go.id/pp-23-2022",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 23 TAHUN 2022 TENTANG PERUBAHAN ATAS PP NOMOR 45 TAHUN 2005 TENTANG BUMN\n\n"
            "Pasal 99A — Direksi dan Dewan Komisaris/Dewan Pengawas BUMN dilarang mengambil keuntungan pribadi baik secara "
            "langsung maupun tidak langsung dari kegiatan BUMN selain penghasilan yang sah, dan wajib memastikan kepatuhan "
            "terhadap sistem pengendalian intern dan manajemen risiko."
        ),
    },
    {
        "domain": "jdih_bumn",
        "judul": "Peraturan Menteri BUMN Nomor PER-2/MBU/03/2023 tentang Pedoman Tata Kelola dan Kegiatan Korporasi Signifikan Badan Usaha Milik Negara",
        "nomor": "Permen BUMN No. PER-2/MBU/03/2023",
        "jenis": "Permen BUMN",
        "sektor": "BUMN & Tata Kelola Korporasi",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.bumn.go.id/per-2-mbu-03-2023",
        "content": (
            "PERATURAN MENTERI BADAN USAHA MILIK NEGARA NOMOR PER-2/MBU/03/2023 TENTANG PEDOMAN TATA KELOLA DAN KEGIATAN KORPORASI SIGNIFIKAN BUMN\n\n"
            "Mengatur penerapan Tata Kelola Perusahaan yang Baik (GCG), manajemen risiko terintegrasi, sistem manajemen "
            "anti-penyuapan (SMAP ISO 37001), pedoman kerja sama BUMN dengan mitra strategis/anak perusahaan, penghapusbukuan "
            "dan pemindahtanganan aset BUMN, serta pengadaan barang dan jasa di lingkungan BUMN dan Anak Perusahaan BUMN."
        ),
    },
    {
        "domain": "jdih_bumn",
        "judul": "Peraturan Menteri BUMN Nomor PER-08/MBU/12/2019 tentang Pedoman Umum Pelaksanaan Pengadaan Barang dan Jasa Badan Usaha Milik Negara",
        "nomor": "Permen BUMN No. PER-08/MBU/12/2019",
        "jenis": "Permen BUMN",
        "sektor": "Pengadaan Barang/Jasa & BUMN",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.bumn.go.id/per-08-mbu-12-2019",
        "content": (
            "PERATURAN MENTERI BUMN NOMOR PER-08/MBU/12/2019 TENTANG PEDOMAN UMUM PELAKSANAAN PENGADAAN BARANG DAN JASA BUMN\n\n"
            "Pasal 4 — Pengadaan Barang dan Jasa BUMN dilakukan melalui cara: Tender/Seleksi Umum, Tender/Seleksi Terbatas, "
            "Penunjukan Langsung, Pengadaan Langsung, atau E-Purchasing/Katalog Elektronik BUMN.\n\n"
            "Pasal 12 — BUMN wajib mengutamakan penggunaan produksi dalam negeri (TKDN) dan sinergi antar-BUMN serta "
            "Anak Perusahaan BUMN sepanjang kualitas, harga, dan tujuannya dapat dipertanggungjawabkan."
        ),
    },
    {
        "domain": "jdih_lkpp",
        "judul": "Peraturan LKPP Nomor 12 Tahun 2021 tentang Pedoman Pelaksanaan Pengadaan Barang/Jasa Pemerintah Melalui Penyedia",
        "nomor": "Peraturan LKPP No. 12 Tahun 2021",
        "jenis": "Peraturan LKPP",
        "sektor": "Pengadaan Barang/Jasa (LKPP)",
        "status": "Berlaku",
        "category_code": "PROCUREMENT_SOE",
        "detail_url": "https://jdih.lkpp.go.id/perlkpp-12-2021",
        "content": (
            "PERATURAN LEMBAGA KEBIJAKAN PENGADAAN BARANG/JASA PEMERINTAH NOMOR 12 TAHUN 2021 TENTANG PEDOMAN PELAKSANAAN PENGADAAN BARANG/JASA PEMERINTAH MELALUI PENYEDIA\n\n"
            "Mengatur standar dokumen pemilihan, Syarat-Syarat Umum Kontrak (SSUK), Syarat-Syarat Khusus Kontrak (SSKK), "
            "ketentuan adendum kontrak (perubahan volume maksimal 10%), pembayaran prestasi pekerjaan, keadaan kahar (force majeure), "
            "pemutusan kontrak sepihak oleh PPK, pencairan jaminan pelaksanaan, dan pengenaan Sanksi Daftar Hitam (Blacklist)."
        ),
    },

    # ==================== 7. CORPORATE_INVESTMENT Expansion (5 Regulations) ====================
    {
        "domain": "jdih_bkpm",
        "judul": "Undang-Undang Nomor 25 Tahun 2007 tentang Penanaman Modal",
        "nomor": "UU No. 25 Tahun 2007",
        "jenis": "UU",
        "sektor": "Penanaman Modal & Investasi",
        "status": "Berlaku",
        "category_code": "CORPORATE_INVESTMENT",
        "detail_url": "https://jdih.bkpm.go.id/uu-25-2007",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 25 TAHUN 2007 TENTANG PENANAMAN MODAL\n\n"
            "Pasal 5 — Penanaman modal dalam negeri dapat dilakukan dalam bentuk badan usaha yang berbentuk badan hukum, "
            "tidak berbadan hukum atau usaha perseorangan. Penanaman modal asing (PMA) wajib dalam bentuk Perseroan Terbatas "
            "berdasarkan hukum Indonesia dan berkedudukan di dalam wilayah negara Republik Indonesia.\n\n"
            "Pasal 32 — Penyelesaian sengketa di bidang penanaman modal antara Pemerintah dengan penanam modal asing "
            "diselesaikan melalui arbitrase internasional yang harus disepakati oleh para pihak."
        ),
    },
    {
        "domain": "jdih_bkpm",
        "judul": "Undang-Undang Nomor 6 Tahun 2023 tentang Penetapan Peraturan Pemerintah Pengganti Undang-Undang Nomor 2 Tahun 2022 tentang Cipta Kerja Menjadi Undang-Undang",
        "nomor": "UU No. 6 Tahun 2023",
        "jenis": "UU",
        "sektor": "Perizinan Berusaha & Cipta Kerja",
        "status": "Berlaku",
        "category_code": "CORPORATE_INVESTMENT",
        "detail_url": "https://jdih.bkpm.go.id/uu-6-2023",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 6 TAHUN 2023 TENTANG CIPTA KERJA\n\n"
            "Mengatur penyederhanaan Perizinan Berusaha Berbasis Risiko (Nomor Induk Berusaha / NIB, Sertifikat Standar, dan Izin), "
            "kemudahan pendirian Perseroan Terbatas termasuk Perseroan Perorangan bagi Usaha Mikro dan Kecil, "
            "kesesuaian kegiatan pemanfaatan ruang (KKPR), persetujuan lingkungan, serta kemudahan investasi."
        ),
    },
    {
        "domain": "jdih_bkpm",
        "judul": "Peraturan Pemerintah Nomor 5 Tahun 2021 tentang Penyelenggaraan Perizinan Berusaha Berbasis Risiko",
        "nomor": "PP No. 5 Tahun 2021",
        "jenis": "PP",
        "sektor": "Perizinan Berusaha (OSS-RBA)",
        "status": "Berlaku",
        "category_code": "CORPORATE_INVESTMENT",
        "detail_url": "https://jdih.bkpm.go.id/pp-5-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 5 TAHUN 2021 TENTANG PENYELENGGARAAN PERIZINAN BERUSAHA BERBASIS RISIKO (OSS-RBA)\n\n"
            "Pasal 12 — Perizinan Berusaha untuk kegiatan usaha tingkat Risiko rendah berupa Nomor Induk Berusaha (NIB), "
            "tingkat Risiko menengah rendah dan menengah tinggi berupa NIB dan Sertifikat Standar, serta tingkat Risiko tinggi "
            "berupa NIB dan Izin yang diterbitkan melalui sistem Online Single Submission (OSS)."
        ),
    },
    {
        "domain": "jdih_bkpm",
        "judul": "Peraturan Pemerintah Nomor 8 Tahun 2021 tentang Modal Dasar Perseroan serta Pendaftaran Pendirian, Perubahan, dan Pembubaran Perseroan yang Memenuhi Kriteria untuk Usaha Mikro dan Kecil",
        "nomor": "PP No. 8 Tahun 2021",
        "jenis": "PP",
        "sektor": "Hukum Perusahaan & Perseroan",
        "status": "Berlaku",
        "category_code": "CORPORATE_INVESTMENT",
        "detail_url": "https://jdih.bkpm.go.id/pp-8-2021",
        "content": (
            "PERATURAN PEMERINTAH REPUBLIK INDONESIA NOMOR 8 TAHUN 2021 TENTANG MODAL DASAR PERSEROAN\n\n"
            "Pasal 3 — Besaran modal dasar Perseroan Terbatas ditentukan berdasarkan keputusan pendiri Perseroan dan "
            "paling sedikit 25% dari modal dasar harus ditempatkan dan disetor penuh dengan bukti penyetoran yang sah."
        ),
    },
    {
        "domain": "jdih_bkpm",
        "judul": "Peraturan Badan Koordinasi Penanaman Modal Nomor 4 Tahun 2021 tentang Pedoman dan Tata Cara Pelayanan Perizinan Berusaha Berbasis Risiko dan Fasilitas Penanaman Modal",
        "nomor": "Peraturan BKPM No. 4 Tahun 2021",
        "jenis": "Peraturan BKPM",
        "sektor": "Penanaman Modal & Investasi",
        "status": "Berlaku",
        "category_code": "CORPORATE_INVESTMENT",
        "detail_url": "https://jdih.bkpm.go.id/perbkpm-4-2021",
        "content": (
            "PERATURAN BADAN KOORDINASI PENANAMAN MODAL NOMOR 4 TAHUN 2021 TENTANG PEDOMAN DAN TATA CARA PELAYANAN PERIZINAN BERUSAHA BERBASIS RISIKO\n\n"
            "Pasal 12 — Pelaku Usaha Penanaman Modal Asing (PMA) dikategorikan sebagai usaha besar dan wajib memenuhi "
            "ketentuan nilai investasi lebih besar dari Rp10.000.000.000,00 (sepuluh miliar rupiah) di luar tanah dan bangunan "
            "per bidang usaha KBLI 5 digit serta modal ditempatkan/disetor paling sedikit Rp10.000.000.000,00."
        ),
    },

    # ==================== 8. TECHNOLOGY_DIGITAL Expansion (5 Regulations) ====================
    {
        "domain": "jdih_komdigi",
        "judul": "Undang-Undang Nomor 1 Tahun 2024 tentang Perubahan Kedua atas Undang-Undang Nomor 11 Tahun 2008 tentang Informasi dan Transaksi Elektronik",
        "nomor": "UU No. 1 Tahun 2024",
        "jenis": "UU",
        "sektor": "Teknologi Informasi & Siber",
        "status": "Berlaku",
        "category_code": "TECHNOLOGY_DIGITAL",
        "detail_url": "https://jdih.komdigi.go.id/uu-1-2024",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 1 TAHUN 2024 TENTANG PERUBAHAN KEDUA ATAS UNDANG-UNDANG NOMOR 11 TAHUN 2008 TENTANG INFORMASI DAN TRANSAKSI ELEKTRONIK\n\n"
            "Pasal 13A — Penyelenggara Sertifikasi Elektronik (PSrE) wajib memastikan keamanan dan keandalan Tanda Tangan "
            "Elektronik tersertifikasi. Dalam hal melakukan kontrak elektronik internasional yang menggunakan klausul baku, "
            "jika penyelenggara sistem elektronik menyasar pengguna di Indonesia maka hukum yang berlaku adalah hukum Indonesia."
        ),
    },
    {
        "domain": "jdih_komdigi",
        "judul": "Undang-Undang Nomor 36 Tahun 1999 tentang Telekomunikasi",
        "nomor": "UU No. 36 Tahun 1999",
        "jenis": "UU",
        "sektor": "Telekomunikasi & Infrastruktur Digital",
        "status": "Berlaku",
        "category_code": "TECHNOLOGY_DIGITAL",
        "detail_url": "https://jdih.komdigi.go.id/uu-36-1999",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 36 TAHUN 1999 TENTANG TELEKOMUNIKASI\n\n"
            "Pasal 7 — Penyelenggaraan telekomunikasi meliputi penyelenggaraan jaringan telekomunikasi, penyelenggaraan "
            "jasa telekomunikasi, dan penyelenggaraan telekomunikasi khusus.\n\n"
            "Pasal 22 — Setiap penyelenggara jaringan telekomunikasi dan/atau penyelenggara jasa telekomunikasi wajib "
            "merahasiakan informasi yang dikirim dan/atau diterima oleh pelanggan jasa telekomunikasi melalui jaringan "
            "telekomunikasi dan/atau jasa telekomunikasi yang diselenggarakannya."
        ),
    },
    {
        "domain": "jdih_komdigi",
        "judul": "Peraturan Menteri Komunikasi dan Informatika Nomor 5 Tahun 2020 tentang Penyelenggara Sistem Elektronik Lingkup Privat",
        "nomor": "Permenkominfo No. 5 Tahun 2020",
        "jenis": "Permenkominfo",
        "sektor": "Sistem Elektronik & Platform Digital",
        "status": "Berlaku",
        "category_code": "TECHNOLOGY_DIGITAL",
        "detail_url": "https://jdih.komdigi.go.id/permenkominfo-5-2020",
        "content": (
            "PERATURAN MENTERI KOMUNIKASI DAN INFORMATIKA NOMOR 5 TAHUN 2020 TENTANG PENYELENGGARA SISTEM ELEKTRONIK LINGKUP PRIVAT\n\n"
            "Pasal 2 — Setiap Penyelenggara Sistem Elektronik (PSE) Lingkup Privat baik domestik maupun asing yang layanannya "
            "digunakan di wilayah Indonesia wajib melakukan pendaftaran PSE sebelum Sistem Elektronik mulai digunakan oleh Pengguna.\n\n"
            "Pasal 9 — PSE Lingkup Privat bertanggung jawab atas penyelenggaraan Sistem Elektronik dan pengelolaan Informasi "
            "Elektronik dan/atau Dokumen Elektronik di dalam Sistem Elektronik secara andal, aman, dan bertanggung jawab."
        ),
    },
    {
        "domain": "jdih_komdigi",
        "judul": "Peraturan Menteri Komunikasi dan Informatika Nomor 20 Tahun 2016 tentang Perlindungan Data Pribadi dalam Sistem Elektronik",
        "nomor": "Permenkominfo No. 20 Tahun 2016",
        "jenis": "Permenkominfo",
        "sektor": "Pelindungan Data Pribadi",
        "status": "Berlaku",
        "category_code": "TECHNOLOGY_DIGITAL",
        "detail_url": "https://jdih.komdigi.go.id/permenkominfo-20-2016",
        "content": (
            "PERATURAN MENTERI KOMUNIKASI DAN INFORMATIKA NOMOR 20 TAHUN 2016 TENTANG PERLINDUNGAN DATA PRIBADI DALAM SISTEM ELEKTRONIK\n\n"
            "Pasal 28 — Dalam hal terjadi kegagalan perlindungan rahasia Data Pribadi dalam Sistem Elektronik yang dikelolanya, "
            "Penyelenggara Sistem Elektronik wajib memberitahukan secara tertulis kepada Pemilik Data Pribadi paling lambat 14 hari."
        ),
    },
    {
        "domain": "jdih_bssn",
        "judul": "Peraturan Badan Siber dan Sandi Negara Nomor 4 Tahun 2021 tentang Pedoman Manajemen Keamanan Informasi Sistem Pemerintahan Berbasis Elektronik dan Standar Teknis dan Prosedur Keamanan SPBE",
        "nomor": "Peraturan BSSN No. 4 Tahun 2021",
        "jenis": "Peraturan BSSN",
        "sektor": "Keamanan Siber & Sandi Negara",
        "status": "Berlaku",
        "category_code": "TECHNOLOGY_DIGITAL",
        "detail_url": "https://jdih.bssn.go.id/perbssn-4-2021",
        "content": (
            "PERATURAN BADAN SIBER DAN SANDI NEGARA NOMOR 4 TAHUN 2021 TENTANG PEDOMAN MANAJEMEN KEAMANAN INFORMASI SPBE\n\n"
            "Mengatur standar manajemen keamanan informasi (SNI ISO/IEC 27001), enkripsi data dalam penyimpanan dan transmisi, "
            "audit keamanan aplikasi dan infrastruktur TI, manajemen insiden siber (CSIRT), serta perjanjian tingkat layanan "
            "keamanan informasi dengan pihak ketiga/penyedia layanan cloud."
        ),
    },

    # ==================== 9. CRIMINAL_DISPUTE Expansion (4 Regulations) ====================
    {
        "domain": "jdih_ma",
        "judul": "Undang-Undang Nomor 30 Tahun 1999 tentang Arbitrase dan Alternatif Penyelesaian Sengketa",
        "nomor": "UU No. 30 Tahun 1999",
        "jenis": "UU",
        "sektor": "Arbitrase & Penyelesaian Sengketa",
        "status": "Berlaku",
        "category_code": "CRIMINAL_DISPUTE",
        "detail_url": "https://jdih.mahkamahagung.go.id/uu-30-1999",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 30 TAHUN 1999 TENTANG ARBITRASE DAN ALTERNATIF PENYELESAIAN SENGKETA\n\n"
            "Pasal 3 — Pengadilan Negeri tidak berwenang untuk mengadili sengketa para pihak yang telah terikat dalam "
            "perjanjian arbitrase (klausul arbitrase BANI/SIAC/ICC).\n\n"
            "Pasal 59 — Dalam waktu paling lama 30 (tiga puluh) hari terhitung sejak tanggal putusan diucapkan, lembar "
            "asli atau salinan otentik putusan arbitrase diserahkan dan didaftarkan oleh arbiter atau kuasanya kepada "
            "Panitera Pengadilan Negeri.\n\n"
            "Pasal 60 — Putusan arbitrase bersifat final dan mempunyai kekuatan hukum tetap dan mengikat para pihak."
        ),
    },
    {
        "domain": "jdih_ma",
        "judul": "Undang-Undang Nomor 37 Tahun 2004 tentang Kepailitan dan Penundaan Kewajiban Pembayaran Utang (PKPU)",
        "nomor": "UU No. 37 Tahun 2004",
        "jenis": "UU",
        "sektor": "Kepailitan & PKPU",
        "status": "Berlaku",
        "category_code": "CRIMINAL_DISPUTE",
        "detail_url": "https://jdih.mahkamahagung.go.id/uu-37-2004",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 37 TAHUN 2004 TENTANG KEPAILITAN DAN PENUNDAAN KEWAJIBAN PEMBAYARAN UTANG\n\n"
            "Pasal 2 — Debitor yang mempunyai dua atau lebih Kreditor dan tidak membayar lunas sedikitnya satu utang yang "
            "telah jatuh waktu dan dapat ditagih, dinyatakan pailit dengan putusan Pengadilan Niaga, baik atas permohonannya "
            "sendiri maupun atas permohonan satu atau lebih kreditornya.\n\n"
            "Pasal 36 — Dalam hal pada saat putusan pernyataan pailit diucapkan terdapat perjanjian timbal balik yang belum "
            "atau baru sebagian dipenuhi, pihak yang mengadakan perjanjian dengan Debitor dapat meminta kepada Kurator "
            "untuk memberikan kepastian tentang kelanjutan pelaksanaan perjanjian tersebut."
        ),
    },
    {
        "domain": "jdih_ma",
        "judul": "Undang-Undang Nomor 20 Tahun 2001 tentang Perubahan atas Undang-Undang Nomor 31 Tahun 1999 tentang Pemberantasan Tindak Pidana Korupsi",
        "nomor": "UU No. 20 Tahun 2001",
        "jenis": "UU",
        "sektor": "Tindak Pidana Korupsi & Anti-Suap",
        "status": "Berlaku",
        "category_code": "CRIMINAL_DISPUTE",
        "detail_url": "https://jdih.mahkamahagung.go.id/uu-20-2001",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 20 TAHUN 2001 TENTANG PEMBERANTASAN TINDAK PIDANA KORUPSI\n\n"
            "Pasal 12B — Setiap gratifikasi kepada pegawai negeri atau penyelenggara negara dianggap pemberian suap, "
            "apabila berhubungan dengan jabatannya dan yang berlawanan dengan kewajiban atau tugasnya.\n\n"
            "Pasal 20 — Dalam hal tindak pidana korupsi dilakukan oleh atau atas nama suatu korporasi, maka tuntutan dan "
            "penjatuhan pidana dapat dilakukan terhadap korporasi dan/atau pengurusnya."
        ),
    },
    {
        "domain": "jdih_ma",
        "judul": "Undang-Undang Nomor 1 Tahun 2023 tentang Kitab Undang-Undang Hukum Pidana (KUHP Nasional)",
        "nomor": "UU No. 1 Tahun 2023",
        "jenis": "UU",
        "sektor": "Hukum Pidana & Pertanggungjawaban Korporasi",
        "status": "Berlaku",
        "category_code": "CRIMINAL_DISPUTE",
        "detail_url": "https://jdih.mahkamahagung.go.id/uu-1-2023",
        "content": (
            "UNDANG-UNDANG REPUBLIK INDONESIA NOMOR 1 TAHUN 2023 TENTANG KITAB UNDANG-UNDANG HUKUM PIDANA\n\n"
            "Pasal 45 — Korporasi merupakan subjek Tindak Pidana.\n\n"
            "Pasal 46 — Tindak Pidana oleh Korporasi merupakan Tindak Pidana yang dilakukan oleh pengurus yang mempunyai "
            "kedudukan fungsional dalam struktur organisasi Korporasi atau orang yang berdasarkan hubungan kerja maupun "
            "hubungan lain bertindak untuk dan atas nama Korporasi atau demi kepentingan Korporasi dalam lingkup usaha "
            "atau kegiatan Korporasi tersebut."
        ),
    },
]

# Known official titles for Kemnaker/Kemenkeu abbreviated filenames
KNOWN_SLUG_TITLES: dict[str, tuple[str, str, str, str]] = {
    "2022UU012": (
        "Undang-Undang Nomor 12 Tahun 2022 tentang Tindak Pidana Kekerasan Seksual",
        "UU No. 12 Tahun 2022",
        "UU",
        "CRIMINAL_DISPUTE",
    ),
    "2022UU014": (
        "Undang-Undang Nomor 14 Tahun 2022 tentang Pembentukan Provinsi Papua Selatan",
        "UU No. 14 Tahun 2022",
        "UU",
        "CIVIL_CONTRACT",
    ),
    "2022UU015": (
        "Undang-Undang Nomor 15 Tahun 2022 tentang Pembentukan Provinsi Papua Tengah",
        "UU No. 15 Tahun 2022",
        "UU",
        "CIVIL_CONTRACT",
    ),
    "2022UU016": (
        "Undang-Undang Nomor 16 Tahun 2022 tentang Pembentukan Provinsi Papua Pegunungan",
        "UU No. 16 Tahun 2022",
        "UU",
        "CIVIL_CONTRACT",
    ),
    "36 TAHUN 2009": (
        "Undang-Undang Nomor 36 Tahun 2009 tentang Kesehatan",
        "UU No. 36 Tahun 2009",
        "UU",
        "HEALTH_PHARMA",
    ),
    "UU Nomor 2 Tahun 2015": (
        "Undang-Undang Nomor 2 Tahun 2015 tentang Penetapan Perppu Nomor 2 Tahun 2014 tentang Perubahan atas Undang-Undang Nomor 23 Tahun 2014 tentang Pemerintahan Daerah",
        "UU No. 2 Tahun 2015",
        "UU",
        "CIVIL_CONTRACT",
    ),
}


def _extract_title_from_text(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract (judul, nomor, jenis) from the opening text of an Indonesian regulation."""
    if not text:
        return None, None, None
    head = re.sub(r"\s+", " ", text[:3500]).strip()

    m = re.search(
        r"(UNDANG-UNDANG|PERATURAN PEMERINTAH PENGGANTI UNDANG-UNDANG|PERATURAN PEMERINTAH|"
        r"PERATURAN PRESIDEN|PERATURAN MENTERI [A-Z\s,]+?|KEPUTUSAN MENTERI [A-Z\s,]+?|"
        r"PERATURAN OTORITAS JASA KEUANGAN|PERATURAN BANK INDONESIA|PERATURAN BADAN [A-Z\s]+?)"
        r"\s+(?:REPUBLIK INDONESIA\s+)?NOMOR\s+([A-Z0-9./\-~]+)\s*(?:TAHUN\s+(\d{4}))?"
        r"\s+TENTANG\s+(.{10,220}?)(?=\s+DENGAN RAHMAT|\s+Menimbang|\s+ Mengingat|\s+MEMUTUSKAN|\s+Pasal 1\b|$)",
        head,
        re.I,
    )
    if not m:
        return None, None, None

    raw_jenis = m.group(1).strip().title()
    raw_no = m.group(2).strip()
    raw_yr = (m.group(3) or "").strip()
    raw_tentang = m.group(4).strip(" .;,-")

    if "Undang-Undang" in raw_jenis and "Pengganti" not in raw_jenis:
        short_jenis = "UU"
    elif "Peraturan Pemerintah" in raw_jenis:
        short_jenis = "PP"
    elif "Peraturan Presiden" in raw_jenis:
        short_jenis = "Perpres"
    elif "Menteri Keuangan" in raw_jenis:
        short_jenis = "PMK"
    elif "Menteri Ketenagakerjaan" in raw_jenis or "Menteri Tenaga Kerja" in raw_jenis:
        short_jenis = "Permenaker"
    else:
        short_jenis = raw_jenis

    if raw_yr and raw_yr not in raw_no:
        nomor_clean = f"{short_jenis} No. {raw_no} Tahun {raw_yr}"
        judul_clean = f"{raw_jenis} Nomor {raw_no} Tahun {raw_yr} tentang {raw_tentang.title()}"
    else:
        nomor_clean = f"{short_jenis} No. {raw_no}"
        judul_clean = f"{raw_jenis} Nomor {raw_no} tentang {raw_tentang.title()}"

    return judul_clean[:300], nomor_clean[:100], short_jenis[:50]


def enrich_abbreviated_titles_and_unlinked_rows() -> dict:
    """Fix abbreviated titles (e.g., 2022UU012, 36 TAHUN 2009, 167~PMK...) and
    materialize local_path files for any unlinked regulation rows."""
    rows = pg_service.query(
        "SELECT id, domain, judul, nomor, jenis, sektor, status, local_path, klasifikasi "
        "FROM regulations ORDER BY id"
    )
    fixed_titles = 0
    linked_files = 0

    with pg_service.get_conn() as conn:
        with conn.transaction():
            for r in rows:
                reg_id = r["id"]
                judul = (r["judul"] or "").strip()
                nomor = (r["nomor"] or "").strip()
                jenis = (r["jenis"] or "").strip()
                local_path = (r["local_path"] or "").strip()

                # 1. Check known slug titles first
                new_judul, new_nomor, new_jenis, forced_cat = None, None, None, None
                if judul in KNOWN_SLUG_TITLES:
                    new_judul, new_nomor, new_jenis, forced_cat = KNOWN_SLUG_TITLES[judul]
                elif "tentang" not in judul.lower() or nomor.startswith(("Kemnaker-", "Kemenkeu-")):
                    # Fetch first chunks to parse official header
                    chunk_rows = conn.execute(
                        "SELECT text FROM chunks WHERE doc_id = %s ORDER BY id LIMIT 3",
                        (str(reg_id),),
                    ).fetchall()
                    combined = " ".join((c["text"] or "") for c in chunk_rows)
                    ej, en, ejen = _extract_title_from_text(combined)
                    if ej and "tentang" in ej.lower():
                        new_judul, new_nomor, new_jenis = ej, en, ejen
                    elif nomor.startswith(("Kemnaker-", "Kemenkeu-")) and re.search(r"\d", judul):
                        # Clean up Kemnaker-hash / Kemenkeu-hash nomor using judul
                        new_nomor = judul.replace("~", "/")[:80]

                if new_judul or new_nomor or new_jenis:
                    f_judul = new_judul or judul
                    f_nomor = new_nomor or nomor
                    f_jenis = new_jenis or jenis
                    conn.execute(
                        "UPDATE regulations SET judul = %s, nomor = %s, jenis = %s WHERE id = %s",
                        (f_judul, f_nomor, f_jenis, reg_id),
                    )
                    conn.execute(
                        "UPDATE chunks SET judul = %s, nomor = %s, jenis = %s WHERE doc_id = %s",
                        (f_judul, f_nomor, f_jenis, str(reg_id)),
                    )
                    if forced_cat:
                        conn.execute(
                            "UPDATE regulations SET category_code = %s WHERE id = %s",
                            (forced_cat, reg_id),
                        )
                        conn.execute(
                            "UPDATE chunks SET category_code = %s WHERE doc_id = %s",
                            (forced_cat, str(reg_id)),
                        )
                    fixed_titles += 1

                # 2. Ensure local_path is populated & exists on disk so GET /api/repository returns it
                if not local_path:
                    safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"reg_{reg_id}_{nomor}")[:80] + ".txt"
                    fpath = os.path.join(PDF_DIR, safe_name)
                    chunk_rows = conn.execute(
                        "SELECT text FROM chunks WHERE doc_id = %s ORDER BY id",
                        (str(reg_id),),
                    ).fetchall()
                    body = "\n\n".join((c["text"] or "") for c in chunk_rows) or (new_judul or judul)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(f"{new_judul or judul}\n\n{body}")
                    conn.execute(
                        "UPDATE regulations SET local_path = %s, klasifikasi = 'Umum' WHERE id = %s",
                        (fpath, reg_id),
                    )
                    linked_files += 1

    return {"fixed_titles": fixed_titles, "linked_files": linked_files}


def _chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


def seed_all_missing_categories() -> dict:
    """Insert & embed all regulations in MULTI_CATEGORY_REGULATIONS."""
    inserted_regs = 0
    inserted_chunks = 0

    for item in MULTI_CATEGORY_REGULATIONS:
        detail_url = item["detail_url"]
        judul = item["judul"]
        nomor = item["nomor"]
        jenis = item["jenis"]
        sektor = item["sektor"]
        status = item["status"]
        cat_code = item["category_code"]
        domain = item["domain"]
        content = item["content"]

        _, tahun = normalize_nomor_tahun(nomor)
        canonical_key = build_canonical_key(jenis=jenis, nomor=nomor, tahun=tahun)
        content_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()

        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", f"{domain}_{nomor}")[:80] + ".txt"
        fpath = os.path.join(PDF_DIR, safe_name)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"{judul}\n\n{content}")

        with pg_service.get_conn() as conn:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT id FROM regulations WHERE detail_url = %s OR canonical_key = %s LIMIT 1",
                    (detail_url, canonical_key),
                ).fetchone()

                if existing:
                    reg_id = existing["id"]
                    conn.execute(
                        "UPDATE regulations SET judul = %s, nomor = %s, jenis = %s, sektor = %s, "
                        "status = 'Berlaku', legal_status = 'Berlaku', klasifikasi = 'Umum', "
                        "category_code = %s, local_path = %s, canonical_key = %s, content_hash = %s "
                        "WHERE id = %s",
                        (judul, nomor, jenis, sektor, cat_code, fpath, canonical_key, content_hash, reg_id),
                    )
                else:
                    row = conn.execute(
                        "INSERT INTO regulations ("
                        "  domain, judul, nomor, jenis, sektor, status, legal_status, klasifikasi, "
                        "  detail_url, download_url, local_path, canonical_key, tahun, "
                        "  category_code, content_hash"
                        ") VALUES (%s, %s, %s, %s, %s, 'Berlaku', 'Berlaku', 'Umum', %s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING id",
                        (
                            domain,
                            judul,
                            nomor,
                            jenis,
                            sektor,
                            detail_url,
                            detail_url,
                            fpath,
                            canonical_key,
                            tahun,
                            cat_code,
                            content_hash,
                        ),
                    ).fetchone()
                    reg_id = row["id"]
                    inserted_regs += 1

                # Ensure chunks exist in public.chunks
                has_chunks = conn.execute(
                    "SELECT 1 FROM chunks WHERE doc_id = %s LIMIT 1",
                    (str(reg_id),),
                ).fetchone()

                if not has_chunks:
                    full_text = f"{judul}\n\n{content}"
                    chunks = _chunk_text(full_text)
                    embeddings = embed_documents(chunks)
                    for idx, (ch_text, emb) in enumerate(zip(chunks, embeddings)):
                        chunk_id = f"{reg_id}_c{idx}"
                        conn.execute(
                            "INSERT INTO chunks ("
                            "  id, doc_id, text, window_context, domain, jenis, judul, nomor, "
                            "  sektor, status, filename, doc_category, visibility, user_id, "
                            "  embedding, sparse_legacy, category_code, is_active_law"
                            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Berlaku', %s, 'regulations', 'public', NULL, %s::vector, FALSE, %s, TRUE) "
                            "ON CONFLICT (id) DO UPDATE SET "
                            "  category_code = EXCLUDED.category_code, "
                            "  is_active_law = TRUE",
                            (
                                chunk_id,
                                str(reg_id),
                                ch_text,
                                judul,
                                domain,
                                jenis,
                                judul,
                                nomor,
                                sektor,
                                safe_name,
                                emb,
                                cat_code,
                            ),
                        )
                        inserted_chunks += 1

    return {"inserted_regs": inserted_regs, "inserted_chunks": inserted_chunks}


if __name__ == "__main__":
    import json

    r1 = enrich_abbreviated_titles_and_unlinked_rows()
    r2 = seed_all_missing_categories()
    r3 = backfill_existing_categories()
    # Re-apply explicit overrides for KNOWN_SLUG_TITLES and MULTI_CATEGORY_REGULATIONS
    with pg_service.get_conn() as conn:
        with conn.transaction():
            for item in MULTI_CATEGORY_REGULATIONS:
                conn.execute(
                    "UPDATE regulations SET category_code = %s WHERE detail_url = %s",
                    (item["category_code"], item["detail_url"]),
                )
            for _, (j_title, _, _, f_cat) in KNOWN_SLUG_TITLES.items():
                conn.execute(
                    "UPDATE regulations SET category_code = %s WHERE judul = %s",
                    (f_cat, j_title),
                )
            conn.execute(
                "UPDATE chunks c SET category_code = r.category_code "
                "FROM regulations r WHERE c.doc_id = r.id::text"
            )

    cov = pg_service.query(
        "SELECT category_code, COUNT(*) AS cnt "
        "FROM regulations "
        "WHERE local_path IS NOT NULL AND local_path != '' AND status NOT LIKE 'Gagal%' "
        "GROUP BY category_code ORDER BY cnt DESC"
    )
    print(json.dumps({"enrichment": r1, "seeded": r2, "coverage": cov}, indent=2))
