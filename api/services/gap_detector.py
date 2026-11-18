"""Reverse-RAG Missing & Incomplete Clause Detector.

Detects critical commercial and regulatory clauses that are omitted or only
vaguely referenced in an uploaded contract. Unlike standard RAG (which only
evaluates clauses that exist), this module probes the contract's own extracted
clauses against a document-type-specific Expected Clause Playbook and
adjudicates all applicable rules in a single batched LLM call.
"""
from __future__ import annotations

import re
from typing import Any

from services.llm_client import call_llm, extract_json

ARCHETYPES = (
    "PKS", "NDA", "RESELLER", "DSA", "DPA", "MSA", "SLA",
    "SOW", "SPK", "PO", "PROCUREMENT", "MOU", "ADDENDUM",
    "BAST", "LICENSE", "EMPLOYMENT", "LEASE", "GENERAL",
)

PLAYBOOK_RULES: list[dict[str, Any]] = [
    {
        "clause_key": "scope_deliverables_sla",
        "gap_id": "gap_scope_deliverables_sla",
        "clause_title": "Scope of Cooperation, Deliverables & SLA",
        "clause_title_id": "Ruang Lingkup Kerja Sama, Hak/Kewajiban & SLA",
        "applicable_archetypes": ("PKS", "MSA", "SLA", "SOW", "SPK", "DSA", "RESELLER", "PROCUREMENT"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "ruang lingkup", "scope of work", "objek perjanjian", "hak dan kewajiban",
            "deliverables", "sla", "service level", "tingkat layanan", "spesifikasi teknis",
            "tujuan kerja sama", "maksud dan tujuan", "uraian pekerjaan",
        ],
        "default_why_dangerous": (
            "Tanpa batasan ruang lingkup, pembagian peran, dan standar tingkat layanan (SLA) yang terukur, "
            "para pihak berisiko mengalami sengketa mengenai batas tanggung jawab operasional dan standar penerimaan hasil kerja."
        ),
        "default_advisory_guidance": (
            "Tambahkan rincian ruang lingkup kerja sama, matriks hak dan kewajiban masing-masing pihak, "
            "serta target SLA atau indikator kinerja utama (KPI) beserta konsekuensi jika tidak tercapai."
        ),
        "recommended_elements": [
            "Uraian spesifik objek kerja sama dan deliverables masing-masing pihak",
            "Target tingkat layanan (SLA) atau standar mutu dan waktu penyelesaian",
            "Mekanisme perubahan ruang lingkup (Change Request / Addendum tertulis)",
        ],
        "default_anchor": "Setelah Pasal Maksud dan Tujuan / Ruang Lingkup",
    },
    {
        "clause_key": "margin_revenue_split",
        "gap_id": "gap_margin_revenue_split",
        "clause_title": "Margin & Revenue Split Disclosure",
        "clause_title_id": "Pengaturan Pembagian Margin & Bagi Hasil Pendapatan",
        "applicable_archetypes": ("PKS", "DSA", "RESELLER"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "margin", "revenue share", "revenue split", "bagi hasil", "pembagian pendapatan",
            "skema bagi hasil", "komisi", "commission", "fee split", "net revenue", "gross revenue",
            "pendapatan bersih", "pendapatan kotor", "diskon mitra", "reseller margin", "profit share",
            "biaya jasa", "imbalan jasa", "nilai kontrak", "harga",
        ],
        "default_why_dangerous": (
            "Dalam perjanjian kerja sama komersial atau distribusi, ketidakjelasan formula pembagian pendapatan/margin "
            "serta komponen biaya pengurang berisiko menimbulkan perselisihan rekonsiliasi keuangan dan pengakuan pendapatan."
        ),
        "default_advisory_guidance": (
            "Cantumkan skema komersial secara eksplisit dalam pasal Ketentuan Keuangan atau Lampiran Komersial, "
            "termasuk persentase pembagian, definisi pendapatan bersih, siklus rekonsiliasi, dan hak audit."
        ),
        "recommended_elements": [
            "Persentase bagi hasil / margin atau skema tarif yang disepakati",
            "Definisi pendapatan kotor vs. bersih dan komponen biaya yang dapat dikurangkan",
            "Jadwal rekonsiliasi data transaksi dan hak audit laporan keuangan terkait",
        ],
        "default_anchor": "Setelah Pasal Ruang Lingkup / Komersial",
    },
    {
        "clause_key": "payment_invoicing_terms",
        "gap_id": "gap_payment_invoicing_terms",
        "clause_title": "Payment, Invoicing & Tax Terms",
        "clause_title_id": "Ketentuan Pembayaran, Penagihan (BAST) & Pajak",
        "applicable_archetypes": ("PKS", "DSA", "RESELLER", "MSA", "SOW", "SPK", "PO", "PROCUREMENT", "LICENSE", "LEASE"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "pembayaran", "payment", "invoice", "invoicing", "penagihan", "faktur", "tagihan",
            "net 30", "hari kalender", "hari kerja", "bast", "berita acara", "pajak", "ppn", "pph",
            "denda keterlambatan", "late payment", "rekening", "jatuh tempo", "due date",
        ],
        "default_why_dangerous": (
            "Ketiadaan tenggat pembayaran, syarat dokumen penagihan (seperti BAST/faktur pajak), dan alokasi PPN/PPh "
            "berpotensi menunda arus kas serta memicu sengketa pemotongan pajak (withholding tax)."
        ),
        "default_advisory_guidance": (
            "Tetapkan jangka waktu pembayaran yang pasti sejak invoice lengkap diterima, daftar dokumen pendukung "
            "penagihan, tanggung jawab PPN/PPh, serta denda atau hak penangguhan layanan bila terjadi keterlambatan."
        ),
        "recommended_elements": [
            "Batas waktu pembayaran (misal: 30 hari kalender sejak invoice & BAST diterima lengkap)",
            "Dokumen pendukung wajib (BAST, Faktur Pajak, rekapitulasi transaksi) dan rekening tujuan",
            "Pembagian kewajiban pajak (PPN, PPh 23/4(2)) serta sanksi denda keterlambatan pembayaran",
        ],
        "default_anchor": "Setelah Pasal Harga / Nilai Kontrak",
    },
    {
        "clause_key": "uu_pdp_data_protection",
        "gap_id": "gap_uu_pdp_data_protection",
        "clause_title": "Personal Data Protection & Breach Notice (UU PDP)",
        "clause_title_id": "Pelindungan Data Pribadi & Notifikasi Insiden (UU No. 27/2022 PDP)",
        "applicable_archetypes": ("PKS", "DSA", "DPA", "MSA", "LICENSE"),
        "category": "regulatory",
        "severity": "critical",
        "semantic_probes": [
            "data pribadi", "personal data", "uu pdp", "27/2022", "27 tahun 2022",
            "pengendali data", "pemroses data", "data controller", "data processor",
            "kebocoran data", "data breach", "3x24", "3 x 24", "72 jam", "72 hours",
            "perlindungan data", "pelindungan data", "data protection", "privacy", "privasi",
        ],
        "default_why_dangerous": (
            "Berdasarkan UU No. 27 Tahun 2022 tentang Pelindungan Data Pribadi (UU PDP), kerja sama yang melibatkan "
            "pertukaran atau pemrosesan data pribadi wajib mengatur status Pengendali/Pemroses Data serta kewajiban "
            "notifikasi insiden kebocoran data paling lambat 3x24 jam (Pasal 46 UU PDP), dengan ancaman sanksi denda hingga 2% pendapatan tahunan."
        ),
        "default_advisory_guidance": (
            "Tambahkan klausul Pelindungan Data Pribadi yang menetapkan peran Pengendali dan Pemroses Data, "
            "standar keamanan teknis/organisasi, larangan transfer data tanpa dasar hukum, dan notifikasi insiden 3x24 jam."
        ),
        "recommended_elements": [
            "Penetapan peran Pengendali Data Pribadi dan Pemroses Data Pribadi sesuai UU No. 27/2022",
            "Kewajiban notifikasi tertulis paling lambat 3x24 jam apabila terjadi kegagalan pelindungan data",
            "Standar pengamanan data serta kewajiban penghapusan/pengembalian data setelah perjanjian berakhir",
        ],
        "default_anchor": "Setelah Pasal Kerahasiaan",
    },
    {
        "clause_key": "confidentiality_term_exceptions",
        "gap_id": "gap_confidentiality_term_exceptions",
        "clause_title": "Confidentiality Survival & Standard Exceptions",
        "clause_title_id": "Kerahasiaan Informasi, Masa Berlaku & Pengecualian",
        "applicable_archetypes": ("NDA", "MOU", "PKS", "DSA", "DPA", "RESELLER", "MSA", "LICENSE"),
        "category": "commercial",
        "severity": "medium",
        "semantic_probes": [
            "kerahasiaan", "confidential", "confidentiality", "informasi rahasia",
            "survival", "bertahan", "setelah berakhirnya", "pengecualian", "exceptions",
            "public domain", "ranah publik", "perintah pengadilan", "court order", "regulator",
        ],
        "default_why_dangerous": (
            "Kewajiban kerahasiaan tanpa jangka waktu bertahan (survival period) pasca-pengakhiran serta pengecualian "
            "standar (informasi publik atau perintah regulator/pengadilan) dapat menyebabkan perlindungan rahasia dagang "
            "berakhir terlalu cepat atau menyulitkan kepatuhan terhadap otoritas."
        ),
        "default_advisory_guidance": (
            "Tegaskan durasi berlakunya kewajiban kerahasiaan setelah kontrak berakhir (umumnya 3-5 tahun) dan "
            "cantumkan pengecualian untuk informasi yang telah menjadi milik publik maupun pengungkapan wajib hukum."
        ),
        "recommended_elements": [
            "Definisi Informasi Rahasia dan durasi survival pasca-pengakhiran (misal: 3-5 tahun)",
            "Pengecualian standar (domain publik, sudah diketahui sebelumnya, pengembangan independen)",
            "Prosedur pengungkapan wajib berdasarkan perintah pengadilan atau otoritas/regulator berwenang",
        ],
        "default_anchor": "Di dalam atau Setelah Pasal Kerahasiaan",
    },
    {
        "clause_key": "ip_ownership_license",
        "gap_id": "gap_ip_ownership_license",
        "clause_title": "Intellectual Property Ownership & License",
        "clause_title_id": "Kepemilikan & Lisensi Hak Kekayaan Intelektual (HKI)",
        "applicable_archetypes": ("PKS", "DSA", "RESELLER", "MSA", "SOW", "LICENSE"),
        "category": "commercial",
        "severity": "medium",
        "semantic_probes": [
            "kekayaan intelektual", "intellectual property", "hki", "haki", "hak cipta",
            "copyright", "merek", "trademark", "lisensi", "license", "source code",
            "background ip", "foreground ip", "kepemilikan hasil", "paten",
        ],
        "default_why_dangerous": (
            "Tanpa pengaturan HKI yang jelas (UU No. 28/2014 tentang Hak Cipta & UU No. 20/2016 tentang Merek), "
            "kepemilikan atas hasil pengembangan bersama, materi promosi, integrasi sistem, atau penggunaan merek mitra "
            "dapat menjadi sengketa."
        ),
        "default_advisory_guidance": (
            "Pisahkan secara tegas antara HKI Bawaan (Background IP) milik masing-masing pihak dengan HKI Hasil "
            "Kerja Sama (Foreground IP), serta batasi ruang lingkup penggunaan merek atau logo."
        ),
        "recommended_elements": [
            "Penegasan bahwa Background IP tetap menjadi milik eksklusif masing-masing pihak",
            "Status kepemilikan dan hak lisensi atas Foreground IP / hasil pekerjaan bersama",
            "Ketentuan izin tertulis sebelum menggunakan nama, merek dagang, atau logo pihak lain",
        ],
        "default_anchor": "Setelah Pasal Ruang Lingkup / Kerahasiaan",
    },
    {
        "clause_key": "limitation_of_liability",
        "gap_id": "gap_limitation_of_liability",
        "clause_title": "Limitation of Liability & Indemnity Cap",
        "clause_title_id": "Pembatasan Tanggung Jawab (Liability Cap) & Ganti Rugi",
        "applicable_archetypes": ("PKS", "DSA", "DPA", "RESELLER", "MSA", "PROCUREMENT", "PO", "LICENSE"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "limitation of liability", "pembatasan tanggung jawab", "batas tanggung jawab",
            "ganti rugi", "indemnity", "indemnification", "kerugian tidak langsung",
            "consequential damages", "indirect damages", "liability cap", "maksimal tanggung jawab",
        ],
        "default_why_dangerous": (
            "Tanpa klausul pembatasan tanggung jawab, eksposur ganti rugi perusahaan atas wanprestasi, gangguan sistem, "
            "atau kerugian tidak langsung (consequential damages) menjadi tidak terbatas secara finansial."
        ),
        "default_advisory_guidance": (
            "Tetapkan batas maksimal ganti rugi agregat (misal: sebesar total nilai kontrak atau biaya 12 bulan terakhir), "
            "kecualikan kerugian tidak langsung/kehilangan keuntungan, dan atur pengecualian untuk kesengajaan/kelalaian berat."
        ),
        "recommended_elements": [
            "Batas maksimal tanggung jawab finansial (aggregate liability cap) yang proporsional",
            "Pengecualian kerugian tidak langsung, insidental, dan hilangnya potensi keuntungan",
            "Pengecualian batas tanggung jawab untuk pelanggaran kerahasiaan, HKI, penipuan, atau kelalaian berat",
        ],
        "default_anchor": "Setelah Pasal Ganti Rugi / Wanprestasi",
    },
    {
        "clause_key": "termination_for_convenience_cause",
        "gap_id": "gap_termination_for_convenience_cause",
        "clause_title": "Term, Termination Notice, Cure Period & Wind-Down",
        "clause_title_id": "Jangka Waktu, Pengakhiran, Masa Perbaikan & Transisi",
        "applicable_archetypes": ("PKS", "DSA", "DPA", "RESELLER", "MSA", "SLA", "SPK", "EMPLOYMENT", "PROCUREMENT", "LICENSE", "LEASE"),
        "category": "commercial",
        "severity": "medium",
        "semantic_probes": [
            "pengakhiran", "termination", "pemutusan", "pemberitahuan tertulis", "notice period",
            "masa perbaikan", "cure period", "wanprestasi", "material breach",
            "pasca pengakhiran", "wind-down", "handover", "transisi", "1266", "1267",
        ],
        "default_why_dangerous": (
            "Ketentuan pengakhiran yang tidak mengatur masa perbaikan wanprestasi (cure period), pengesampingan Pasal 1266 "
            "KUHPerdata untuk pengakhiran sepihak, serta kewajiban transisi berisiko menghentikan operasional secara mendadak "
            "atau memaksa proses pembatalan melalui putusan pengadilan."
        ),
        "default_advisory_guidance": (
            "Atur alasan pengakhiran karena wanprestasi (dengan masa perbaikan 14-30 hari), pengakhiran tanpa sebab dengan "
            "pemberitahuan tertulis sebelumnya, pengesampingan Pasal 1266 KUHPerdata, dan penyelesaian hak/kewajiban tersisa."
        ),
        "recommended_elements": [
            "Jangka waktu perjanjian, mekanisme perpanjangan, dan masa perbaikan wanprestasi (14-30 hari)",
            "Pengesampingan ketentuan Pasal 1266 KUHPerdata terkait pembatalan perjanjian",
            "Kewajiban penyelesaian pembayaran tertunggak dan serah terima data/aset pasca-pengakhiran",
        ],
        "default_anchor": "Setelah Pasal Jangka Waktu",
    },
    {
        "clause_key": "force_majeure_clause",
        "gap_id": "gap_force_majeure_clause",
        "clause_title": "Force Majeure & Notification Mechanism",
        "clause_title_id": "Keadaan Kahar (Force Majeure) & Mekanisme Pemberitahuan",
        "applicable_archetypes": ("PKS", "DSA", "RESELLER", "MSA", "SOW", "SPK", "PO", "PROCUREMENT", "LICENSE", "LEASE"),
        "category": "commercial",
        "severity": "medium",
        "semantic_probes": [
            "keadaan kahar", "force majeure", "keadaan memaksa", "bencana alam",
            "kahar", "di luar kendali", "beyond reasonable control",
        ],
        "default_why_dangerous": (
            "Tanpa definisi Keadaan Kahar (Pasal 1244-1245 KUHPerdata) serta batas waktu pemberitahuan yang jelas, "
            "salah satu pihak dapat menyalahgunakan alasan keadaan kahar untuk menunda kewajiban pembayaran atau mengakhiri "
            "kontrak secara sepihak."
        ),
        "default_advisory_guidance": (
            "Definisikan peristiwa Keadaan Kahar secara spesifik (tidak termasuk kesulitan finansial), tetapkan batas "
            "waktu pemberitahuan tertulis (misal: 3-7 hari kerja), dan hak pengakhiran jika kahar berlanjut > 30-60 hari."
        ),
        "recommended_elements": [
            "Definisi peristiwa di luar kendali wajar (bencana alam, perang, kebijakan pemerintah/regulasi)",
            "Kewajiban pemberitahuan tertulis disertai bukti resmi dalam jangka waktu tertentu (misal: 7 hari)",
            "Penegasan bahwa kewajiban pembayaran yang telah jatuh tempo tidak dibebaskan dan opsi pengakhiran jika berlarut",
        ],
        "default_anchor": "Sebelum Pasal Penyelesaian Sengketa",
    },
    {
        "clause_key": "dispute_resolution_governing_law",
        "gap_id": "gap_dispute_resolution_governing_law",
        "clause_title": "Governing Law & Dispute Resolution",
        "clause_title_id": "Hukum yang Berlaku & Penyelesaian Sengketa (Arbitrase/Pengadilan)",
        "applicable_archetypes": tuple(a for a in ARCHETYPES if a not in ("BAST", "ADDENDUM")),
        "category": "regulatory",
        "severity": "high",
        "semantic_probes": [
            "penyelesaian sengketa", "dispute resolution", "musyawarah", "arbitrase", "bani",
            "pengadilan negeri", "domisili hukum", "hukum yang berlaku", "governing law",
            "choice of law", "yurisdiksi",
        ],
        "default_why_dangerous": (
            "Ketiadaan klausul pilihan hukum dan forum penyelesaian sengketa (Pengadilan Negeri atau BANI sesuai "
            "UU No. 30/1999) menimbulkan ketidakpastian yurisdiksi dan memperlama proses penegakan hak saat terjadi sengketa."
        ),
        "default_advisory_guidance": (
            "Cantumkan pilihan hukum Negara Republik Indonesia serta mekanisme penyelesaian sengketa berjenjang: "
            "musyawarah mufakat (misal: 30 hari) dilanjutkan ke BANI atau Pengadilan Negeri yang disepakati."
        ),
        "recommended_elements": [
            "Penetapan Hukum Negara Republik Indonesia sebagai hukum yang mengatur perjanjian",
            "Tahap musyawarah untuk mufakat dengan batas waktu tertentu (misal: 30 hari kalender)",
            "Forum penyelesaian akhir yang tegas (Arbitrase BANI sesuai UU No. 30/1999 atau Pengadilan Negeri tertentu)",
        ],
        "default_anchor": "Sebelum Ketentuan Penutup",
    },
    {
        "clause_key": "governing_language_uu24",
        "gap_id": "gap_governing_language_uu24",
        "clause_title": "Governing Language Clause (UU No. 24/2009)",
        "clause_title_id": "Klausul Bahasa yang Berlaku (UU No. 24/2009 & Perpres 63/2019)",
        "applicable_archetypes": ARCHETYPES,
        "requires_bilingual_or_foreign": True,
        "category": "regulatory",
        "severity": "high",
        "semantic_probes": [
            "bahasa indonesia", "governing language", "prevailing language", "uu 24/2009",
            "24 tahun 2009", "bahasa yang berlaku", "bahasa yang mengikat", "dwibahasa", "bilingual",
            "in the event of any inconsistency", "dalam hal terdapat perbedaan penafsiran",
        ],
        "default_why_dangerous": (
            "Pasal 31 UU No. 24 Tahun 2009 dan Perpres No. 63 Tahun 2019 mewajibkan perjanjian yang melibatkan pihak "
            "Indonesia dibuat dalam Bahasa Indonesia; perjanjian berbahasa Inggris/dwibahasa tanpa klausul bahasa yang "
            "mengikat berisiko digugat batal demi hukum."
        ),
        "default_advisory_guidance": (
            "Tambahkan klausul Bahasa yang Berlaku yang menyatakan perjanjian dibuat dalam Bahasa Indonesia (dan Bahasa "
            "Inggris jika dwibahasa) serta menetapkan Bahasa Indonesia sebagai versi yang berlaku apabila terdapat perbedaan penafsiran."
        ),
        "recommended_elements": [
            "Pernyataan bahwa perjanjian dibuat dan ditandatangani dalam Bahasa Indonesia (atau versi dwibahasa)",
            "Penetapan versi Bahasa Indonesia sebagai bahasa yang berlaku jika terjadi perbedaan penafsiran",
            "Rujukan kepatuhan terhadap UU No. 24 Tahun 2009 dan Perpres No. 63 Tahun 2019",
        ],
        "default_anchor": "Sebelum Ketentuan Penutup",
    },
    {
        "clause_key": "nda_confidential_info_definition",
        "gap_id": "gap_nda_confidential_info_definition",
        "clause_title": "Definition, Scope & Permitted Use of Confidential Information",
        "clause_title_id": "Definisi, Cakupan & Batasan Tujuan Informasi Rahasia",
        "applicable_archetypes": ("NDA",),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "definisi informasi rahasia", "informasi rahasia adalah", "confidential information means",
            "tertulis", "lisan", "oral", "elektronik", "rahasia dagang", "trade secret",
            "tujuan yang diizinkan", "permitted purpose", "non-use", "hanya digunakan untuk",
            "penandaan", "confidential", "marked as confidential",
        ],
        "default_why_dangerous": (
            "Dalam NDA, ketidakjelasan definisi apa saja yang termasuk Informasi Rahasia (termasuk informasi lisan/visual "
            "dan data teknis/komersial) serta batasan tujuan penggunaannya (Permitted Purpose) membuat perlindungan rahasia "
            "dagang sulit ditegakkan jika terjadi penyalahgunaan oleh pihak penerima."
        ),
        "default_advisory_guidance": (
            "Cantumkan definisi rinci Informasi Rahasia (mencakup dokumen tertulis, elektronik, visual, dan pengungkapan lisan "
            "yang dikonfirmasi tertulis) serta tegaskan bahwa informasi hanya boleh digunakan untuk Tujuan Kerja Sama yang Disepakati."
        ),
        "recommended_elements": [
            "Cakupan spesifik Informasi Rahasia (teknis, finansial, data pelanggan, kode sumber, strategi bisnis)",
            "Mekanisme pengakuan informasi lisan atau visual (misal: konfirmasi tertulis dalam 7-14 hari)",
            "Kewajiban non-use: larangan menggunakan Informasi Rahasia di luar tujuan evaluasi/transaksi",
        ],
        "default_anchor": "Pada Pasal Definisi / Informasi Rahasia",
    },
    {
        "clause_key": "nda_return_destruction_remedies",
        "gap_id": "gap_nda_return_destruction_remedies",
        "clause_title": "Return/Destruction of Confidential Materials & Remedies",
        "clause_title_id": "Pengembalian/Pemusnahan Informasi Rahasia & Upaya Pemulihan",
        "applicable_archetypes": ("NDA",),
        "category": "commercial",
        "severity": "medium",
        "semantic_probes": [
            "pengembalian", "return", "pemusnahan", "destruction", "destroy", "menghapus",
            "sertifikat pemusnahan", "salinan", "copies", "injunctive relief", "ganti rugi",
            "pemulihan", "remedies", "pelanggaran kerahasiaan",
        ],
        "default_why_dangerous": (
            "Tanpa kewajiban pengembalian atau pemusnahan seluruh salinan Informasi Rahasia saat NDA berakhir atau diminta, "
            "dokumen sensitif tetap berada di sistem pihak lain tanpa kontrol dan bukti pemusnahan."
        ),
        "default_advisory_guidance": (
            "Tambahkan kewajiban mengembalikan atau memusnahkan seluruh Informasi Rahasia beserta salinannya dalam batas waktu "
            "tertentu (misal: 7-14 hari kerja) disertai surat pernyataan pemusnahan, serta hak menuntut ganti rugi/injunction."
        ),
        "recommended_elements": [
            "Kewajiban pengembalian atau pemusnahan seluruh dokumen/file Informasi Rahasia atas permintaan atau saat berakhir",
            "Batas waktu pelaksanaan (misal: 14 hari kalender) dan surat konfirmasi tertulis telah dimusnahkan",
            "Hak atas ganti rugi dan tindakan penghentian segera (injunctive relief) apabila terjadi kebocoran",
        ],
        "default_anchor": "Setelah Pasal Kewajiban Kerahasiaan / Jangka Waktu",
    },
    {
        "clause_key": "reseller_territory_exclusivity_terms",
        "gap_id": "gap_reseller_territory_exclusivity_terms",
        "clause_title": "Reseller Territory, Exclusivity, Quota & Pricing Terms",
        "clause_title_id": "Wilayah Pemasaran, Eksklusivitas, Target Penjualan & Ketentuan Reseller",
        "applicable_archetypes": ("RESELLER", "DSA"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "reseller", "wilayah", "territory", "eksklusif", "non-eksklusif", "exclusive", "non-exclusive",
            "target penjualan", "minimum order", "kuota", "quota", "harga jual", "resale price",
            "sub-reseller", "sub-distributor", "purna jual", "after-sales", "garansi", "warranty",
        ],
        "default_why_dangerous": (
            "Dalam perjanjian reseller/distribusi, ketiadaan batas wilayah pemasaran, status eksklusivitas, target penjualan "
            "minimum, serta aturan penunjukan sub-reseller berisiko memicu konflik kanal penjualan (channel conflict) dan "
            "ketidakjelasan tanggung jawab layanan purna jual kepada pelanggan akhir."
        ),
        "default_advisory_guidance": (
            "Tetapkan wilayah geografis atau segmen pelanggan yang diizinkan, status non-eksklusif/eksklusif, target minimum "
            "penjualan, larangan/syarat penunjukan sub-reseller, serta pembagian tanggung jawab garansi dan dukungan teknis."
        ),
        "recommended_elements": [
            "Penetapan wilayah pemasaran/segmen pelanggan dan status eksklusif atau non-eksklusif",
            "Target penjualan minimum (kuota) per periode dan konsekuensi jika tidak tercapai",
            "Ketentuan harga jual, larangan sub-reseller tanpa izin tertulis, dan batas tanggung jawab purna jual/garansi",
        ],
        "default_anchor": "Setelah Pasal Ruang Lingkup / Penunjukan Reseller",
    },
    {
        "clause_key": "mou_binding_status",
        "gap_id": "gap_mou_binding_status",
        "clause_title": "Binding vs. Non-Binding Status & Validity Period",
        "clause_title_id": "Sifat Mengikat vs. Tidak Mengikat (Non-Binding) & Masa Berlaku MoU",
        "applicable_archetypes": ("MOU",),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "tidak mengikat", "non-binding", "non binding", "mengikat secara hukum", "legally binding",
            "perjanjian definitif", "definitive agreement", "masa berlaku", "validity", "eksklusivitas",
            "biaya masing-masing", "own costs", "nota kesepahaman",
        ],
        "default_why_dangerous": (
            "Tanpa pemisahan tegas antara pasal yang tidak mengikat (niat kerja sama komersial) dan pasal yang mengikat "
            "(kerahasiaan, eksklusivitas, hukum yang berlaku), MoU dapat ditafsirkan sebagai perjanjian definitif yang "
            "memaksa komitmen finansial atau operasional sebelum kontrak final ditandatangani."
        ),
        "default_advisory_guidance": (
            "Tegaskan bahwa ketentuan komersial bersifat non-binding hingga Perjanjian Definitif ditandatangani, kecuali "
            "pasal Kerahasiaan, Eksklusivitas (jika ada), Biaya, dan Hukum yang Berlaku, serta tetapkan batas waktu MoU."
        ),
        "recommended_elements": [
            "Pernyataan tegas sifat non-binding atas rencana kerja sama hingga perjanjian definitif ditandatangani",
            "Daftar pasal yang tetap mengikat (Kerahasiaan, Hukum yang Berlaku, Biaya masing-masing pihak)",
            "Jangka waktu berlaku MoU (misal: 3-6 bulan) dan mekanisme pengakhiran otomatis jika tidak tercapai kesepakatan",
        ],
        "default_anchor": "Pada Pasal Sifat Nota Kesepahaman / Jangka Waktu",
    },
    {
        "clause_key": "addendum_principal_reference",
        "gap_id": "gap_addendum_principal_reference",
        "clause_title": "Principal Agreement Reference, Amended Clauses & Ratification",
        "clause_title_id": "Rujukan Perjanjian Induk, Rincian Perubahan & Keberlakuan Sisa Pasal",
        "applicable_archetypes": ("ADDENDUM",),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "perjanjian induk", "principal agreement", "perjanjian awal", "nomor perjanjian",
            "tetap berlaku", "remain in full force", "mengubah", "amend", "diubah menjadi",
            "urutan prioritas", "order of precedence", "tanggal efektif", "effective date",
        ],
        "default_why_dangerous": (
            "Addendum yang tidak merujuk secara presisi nomor/tanggal Perjanjian Induk, pasal yang diubah, atau tidak "
            "menegaskan keberlakuan sisa ketentuan Perjanjian Induk berisiko menimbulkan inkonsistensi tafsir dan celah hukum."
        ),
        "default_advisory_guidance": (
            "Cantumkan judul, nomor, dan tanggal Perjanjian Induk, sebutkan pasal/ayat yang diubah atau ditambahkan secara "
            "eksplisit, tetapkan tanggal efektif perubahan, dan tegaskan bahwa ketentuan lain tetap berlaku dan mengikat."
        ),
        "recommended_elements": [
            "Identifikasi lengkap Perjanjian Induk (judul, nomor, tanggal penandatanganan, dan para pihak)",
            "Rincian pasal yang diubah/ditambah/dihapus beserta tanggal efektif keberlakuan Addendum",
            "Klausul ratifikasi bahwa seluruh ketentuan lain Perjanjian Induk tetap berlaku penuh dan aturan prioritas jika ada konflik",
        ],
        "default_anchor": "Pada Bagian Pembukaan / Pasal Perubahan",
    },
    {
        "clause_key": "bast_deliverables_signoff",
        "gap_id": "gap_bast_deliverables_signoff",
        "clause_title": "Contract Reference, Itemized Deliverables & Acceptance Sign-Off",
        "clause_title_id": "Rujukan Kontrak, Rincian Hasil Serah Terima & Pernyataan Penerimaan",
        "applicable_archetypes": ("BAST",),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "berita acara", "serah terima", "merujuk pada", "berdasarkan perjanjian", "nomor spk", "nomor po",
            "diterima dengan baik", "lengkap", "kuantitas", "spesifikasi", "punch list", "catatan",
            "masa garansi", "penagihan", "pihak yang menyerahkan", "pihak yang menerima",
        ],
        "default_why_dangerous": (
            "BAST tanpa rujukan nomor kontrak/PO/SPK, daftar rincian barang/jasa yang diserahterimakan, serta status "
            "penerimaan (diterima penuh atau dengan catatan/punch-list) dapat menghambat proses penagihan atau melemahkan "
            "klaim garansi jika ditemukan cacat tersembunyi."
        ),
        "default_advisory_guidance": (
            "Pastikan BAST memuat nomor kontrak/PO/SPK acuan, tabel rincian item/kuantitas/kondisi, tanggal serah terima, "
            "catatan perbaikan (jika ada), serta tanda tangan pejabat berwenang kedua pihak."
        ),
        "recommended_elements": [
            "Nomor dan tanggal kontrak/PO/SPK acuan yang mendasari serah terima",
            "Rincian barang/jasa yang diserahkan (nama, jumlah, spesifikasi/serial number, dan kondisi)",
            "Pernyataan status penerimaan (tanpa/dengan catatan punch-list) serta pemicu masa garansi dan penagihan",
        ],
        "default_anchor": "Pada Badan Berita Acara Serah Terima",
    },
    {
        "clause_key": "dpa_subprocessor_crossborder_security",
        "gap_id": "gap_dpa_subprocessor_crossborder_security",
        "clause_title": "Sub-Processor Control, Security Measures & Cross-Border Transfer",
        "clause_title_id": "Pengaturan Sub-Pemroses, Keamanan Teknis & Transfer Data Lintas Negara",
        "applicable_archetypes": ("DPA", "DSA"),
        "category": "regulatory",
        "severity": "high",
        "semantic_probes": [
            "sub-processor", "sub-pemroses", "subpemroses", "pihak ketiga", "transfer data",
            "lintas negara", "cross-border", "luar negeri", "enkripsi", "encryption",
            "akses kontrol", "audit", "hak audit", "pasal 56", "standar keamanan",
        ],
        "default_why_dangerous": (
            "Dalam DPA, penggunaan sub-pemroses tanpa persetujuan Pengendali Data atau transfer data pribadi ke luar "
            "wilayah Indonesia tanpa dasar hukum Pasal 56 UU PDP No. 27/2022 dapat melanggar regulasi dan memicu sanksi administratif."
        ),
        "default_advisory_guidance": (
            "Atur kewajiban persetujuan tertulis untuk sub-pemroses (dengan kewajiban flow-down), standar keamanan teknis/organisasi, "
            "hak audit kepatuhan, serta syarat transfer data lintas batas sesuai UU PDP."
        ),
        "recommended_elements": [
            "Syarat persetujuan tertulis sebelum menunjuk sub-pemroses dan tanggung jawab penuh Pemroses atas sub-pemroses",
            "Standar pengamanan teknis & organisasi (enkripsi, kontrol akses) serta hak audit Pengendali Data",
            "Mekanisme kepatuhan transfer data lintas negara sesuai Pasal 56 UU No. 27/2022",
        ],
        "default_anchor": "Setelah Pasal Kewajiban Pemroses Data",
    },
    {
        "clause_key": "spk_sow_reference_timeline_penalty",
        "gap_id": "gap_spk_sow_reference_timeline_penalty",
        "clause_title": "Parent Contract Reference, Milestones, Acceptance & Delay Penalty",
        "clause_title_id": "Rujukan Kontrak Induk, Jadwal Milestone, Uji Terima (UAT) & Denda Keterlambatan",
        "applicable_archetypes": ("SPK", "SOW"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "perjanjian induk", "msa", "milestone", "jadwal", "jangka waktu pelaksanaan",
            "uat", "user acceptance", "uji terima", "bast", "denda keterlambatan",
            "liquidated damages", "penalti", "change request", "perubahan pekerjaan",
        ],
        "default_why_dangerous": (
            "SPK/SOW tanpa jadwal milestone yang mengikat, kriteria uji terima (UAT/BAST), serta denda keterlambatan "
            "(liquidated damages) menyulitkan penegakan tenggat waktu penyelesaian pekerjaan dan memicu perselisihan lingkup kerja."
        ),
        "default_advisory_guidance": (
            "Cantumkan rujukan Perjanjian Induk (jika ada), tabel milestone & tenggat waktu, prosedur uji terima (UAT/BAST), "
            "mekanisme Change Request, dan tarif denda keterlambatan maksimal."
        ),
        "recommended_elements": [
            "Tanggal mulai, jadwal milestone/penyelesaian, dan rujukan Perjanjian Induk (jika turunan MSA/PKS)",
            "Kriteria dan prosedur uji terima hasil kerja (UAT / BAST)",
            "Sanksi denda keterlambatan (misal: 1 permil per hari, maks 5%) dan mekanisme Change Request tertulis",
        ],
        "default_anchor": "Setelah Pasal Ruang Lingkup / Jadwal Pelaksanaan",
    },
    {
        "clause_key": "po_item_specs_delivery_terms",
        "gap_id": "gap_po_item_specs_delivery_terms",
        "clause_title": "Item Specifications, Delivery Terms, Inspection & Warranty",
        "clause_title_id": "Spesifikasi Barang/Jasa, Ketentuan Pengiriman, Inspeksi & Garansi",
        "applicable_archetypes": ("PO", "PROCUREMENT"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "spesifikasi", "kuantitas", "harga satuan", "unit price", "pengiriman", "delivery",
            "incoterms", "lokasi pengiriman", "inspeksi", "penolakan", "reject",
            "garansi", "warranty", "cacat", "penggantian",
        ],
        "default_why_dangerous": (
            "PO atau Perjanjian Pengadaan tanpa rincian spesifikasi teknis, tenggat & lokasi pengiriman, hak inspeksi/penolakan "
            "barang cacat, serta masa garansi membuat pembeli menanggung risiko barang tidak sesuai tanpa hak retur yang jelas."
        ),
        "default_advisory_guidance": (
            "Lengkapi PO/Perjanjian Pengadaan dengan rincian item, kuantitas, harga satuan, jadwal & alamat pengiriman, "
            "hak inspeksi & penolakan barang cacat, serta masa garansi penggantian."
        ),
        "recommended_elements": [
            "Rincian item/SKU, spesifikasi teknis, kuantitas, dan harga satuan yang pasti",
            "Batas waktu pengiriman, lokasi serah terima, dan peralihan risiko (risk of loss)",
            "Hak inspeksi, penolakan barang tidak sesuai, dan masa garansi perbaikan/penggantian",
        ],
        "default_anchor": "Pada Bagian Spesifikasi / Ketentuan Pengiriman",
    },
    {
        "clause_key": "sla_metrics_uptime_credits",
        "gap_id": "gap_sla_metrics_uptime_credits",
        "clause_title": "Quantified Uptime/Response Targets & Service Credits",
        "clause_title_id": "Target Uptime, Waktu Respons/Resolusi & Skema Service Credit",
        "applicable_archetypes": ("SLA", "MSA"),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "uptime", "availability", "ketersediaan", "99.", "response time", "resolution time",
            "waktu respons", "waktu penyelesaian", "severity", "p1", "p2", "service credit",
            "kompensasi", "restitusi", "scheduled maintenance", "pemeliharaan terjadwal",
        ],
        "default_why_dangerous": (
            "SLA yang tidak mencantumkan angka target ketersediaan (uptime), matriks waktu respons berdasarkan tingkat "
            "keparahan insiden, serta rumus kompensasi (service credit) tidak dapat ditegakkan secara komersial saat terjadi gangguan."
        ),
        "default_advisory_guidance": (
            "Tetapkan persentase uptime bulanan/tahunan, tabel waktu respons & resolusi per tingkat keparahan (Critical/High/Medium), "
            "pengecualian maintenance terjadwal, dan formula service credit."
        ),
        "recommended_elements": [
            "Target persentase ketersediaan layanan (uptime, misal: 99,9%) dan rumus perhitungannya",
            "Matriks waktu respons (response time) dan penyelesaian (resolution time) per tingkat keparahan",
            "Skema kompensasi / service credit jika target gagal dicapai serta hak pengakhiran untuk pelanggaran berulang",
        ],
        "default_anchor": "Pada Pasal Tingkat Layanan / Kompensasi SLA",
    },
    {
        "clause_key": "license_grant_restrictions",
        "gap_id": "gap_license_grant_restrictions",
        "clause_title": "License Grant Scope, Usage Limits & Restrictions",
        "clause_title_id": "Ruang Lingkup Pemberian Lisensi, Batas Penggunaan & Larangan",
        "applicable_archetypes": ("LICENSE",),
        "category": "commercial",
        "severity": "high",
        "semantic_probes": [
            "pemberian lisensi", "license grant", "non-eksklusif", "non-exclusive", "non-transferable",
            "tidak dapat dialihkan", "jumlah pengguna", "seat", "user", "reverse engineer",
            "rekayasa balik", "sublicense", "sub-lisensi", "wilayah penggunaan",
        ],
        "default_why_dangerous": (
            "Tanpa batasan ruang lingkup lisensi yang tegas (jumlah pengguna/perangkat, wilayah, sifat non-eksklusif & "
            "non-transferable, serta larangan reverse-engineering/sub-lisensi), pemberi atau penerima lisensi menghadapi "
            "risiko penyalahgunaan perangkat lunak atau klaim pelanggaran HKI."
        ),
        "default_advisory_guidance": (
            "Definisikan secara tegas sifat lisensi, batas jumlah pengguna/instansi/wilayah, larangan rekayasa balik dan "
            "sub-lisensi tanpa izin, serta ketentuan audit penggunaan lisensi."
        ),
        "recommended_elements": [
            "Sifat lisensi (non-eksklusif, tidak dapat dialihkan, terbatas untuk penggunaan internal)",
            "Batasan metrik penggunaan (jumlah user/seat, node, wilayah, atau durasi berlangganan)",
            "Larangan modifikasi, dekompilasi/reverse-engineering, dan sub-lisensi kepada pihak ketiga",
        ],
        "default_anchor": "Pada Pasal Pemberian Lisensi & Pembatasan",
    },
]


_TITLE_STOP_RE = re.compile(
    r"^(?:"
    r"pasal\s+\d+|article\s+\d+|bab\s+[ivxlcdm\d]+|"
    r"antara\b|between\b|oleh dan antara\b|"
    r"yang bertanda tangan\b|the undersigned\b|"
    r"pada hari ini\b|on this day\b|this\s+[a-z\s]*agreement\s+is\s+made|"
    r"menimbang\b|whereas\b|recitals\b|latar belakang\b|mukadimah\b"
    r")",
    re.IGNORECASE,
)

# Ordered by specificity so specialized agreements win ties over generic PKS.
_ARCHETYPE_KEYWORDS: list[tuple[str, list[tuple[str, int]]]] = [
    ("ADDENDUM", [
        ("addendum perjanjian", 4), ("amandemen perjanjian", 4),
        ("perubahan perjanjian", 4), ("perpanjangan perjanjian", 4),
        ("side letter", 3), (r"\baddendum\b", 4), (r"\bamandemen\b", 4), (r"\bamendment\b", 4),
    ]),
    ("BAST", [
        ("berita acara serah terima", 5), ("berita acara pemeriksaan", 4),
        ("handover certificate", 4), ("acceptance certificate", 4),
        (r"\bbast\b", 4), (r"\bbap\b", 3),
    ]),
    ("DPA", [
        ("data processing agreement", 5), ("data processing addendum", 5),
        ("perjanjian pemrosesan data", 5), ("pemrosesan data pribadi", 4),
        (r"\bdpa\b", 4),
    ]),
    ("NDA", [
        ("non-disclosure agreement", 4), ("non disclosure agreement", 4),
        ("perjanjian kerahasiaan", 4), ("confidentiality agreement", 4),
        ("non-disclosure", 3), ("non disclosure", 3), ("kerahasiaan", 2),
        (r"\bnda\b", 3),
    ]),
    ("RESELLER", [
        ("reseller agreement", 4), ("perjanjian reseller", 4),
        ("perjanjian keagenan", 4), ("agency agreement", 3),
        ("authorized reseller", 3), ("penunjukan reseller", 3),
        (r"\breseller\b", 3),
    ]),
    ("SPK", [
        ("surat perintah kerja", 5), ("work order", 4),
        (r"\bspk\b", 4),
    ]),
    ("SOW", [
        ("statement of work", 5), ("kerangka acuan kerja", 4),
        ("uraian pekerjaan", 3), ("scope of work", 3),
        (r"\bsow\b", 4), (r"\bkak\b", 3),
    ]),
    ("PO", [
        ("purchase order", 5), ("pesanan pembelian", 4), ("order pembelian", 4),
        (r"\bpo\b", 3),
    ]),
    ("SLA", [
        ("service level agreement", 5), ("perjanjian tingkat layanan", 5),
        ("tingkat layanan", 3), (r"\bsla\b", 4),
    ]),
    ("LICENSE", [
        ("software license agreement", 5), ("perjanjian lisensi", 4),
        ("license agreement", 4), ("saas agreement", 4),
        ("subscription agreement", 4), ("end user license", 4),
        (r"\beula\b", 4), (r"\bsaas\b", 3), (r"\blisensi\b", 2),
    ]),
    ("EMPLOYMENT", [
        ("perjanjian kerja waktu tertentu", 4), ("perjanjian kerja waktu tidak tertentu", 4),
        ("employment agreement", 4), ("perjanjian kerja bersama", 4),
        ("surat pengangkatan karyawan", 4),
        (r"\bpkwt\b", 3), (r"\bpkwtt\b", 3), ("perjanjian kerja", 2),
    ]),
    ("LEASE", [
        ("perjanjian sewa menyewa", 4), ("lease agreement", 4),
        ("perjanjian sewa", 3), ("sewa menyewa", 3),
        ("ruang perkantoran", 2), ("hak sewa", 2), (r"\blease\b", 2),
    ]),
    ("PROCUREMENT", [
        ("purchase agreement", 4), ("perjanjian jual beli", 4),
        ("perjanjian pengadaan barang", 4), ("perjanjian pengadaan", 4),
        ("procurement agreement", 4), ("vendor agreement", 4),
        ("pengadaan barang", 3), (r"\bpengadaan\b", 2),
    ]),
    ("DSA", [
        ("data sharing agreement", 4), ("perjanjian berbagi data", 4),
        ("distribution agreement", 4), ("perjanjian distribusi", 4),
        ("data sharing", 3), ("berbagi data", 3), (r"\bdsa\b", 3),
    ]),
    ("MSA", [
        ("master service agreement", 4), ("perjanjian induk layanan", 4),
        ("managed service", 3), ("kontrak layanan", 3), (r"\bmsa\b", 3),
    ]),
    ("MOU", [
        ("memorandum of understanding", 4), ("nota kesepahaman", 4),
        ("letter of intent", 4), ("term sheet", 3),
        (r"\bmou\b", 4), (r"\bloi\b", 3),
    ]),
    ("PKS", [
        ("perjanjian kerja sama", 3), ("perjanjian kerjasama", 3),
        ("perjanjian kemitraan", 3), ("partnership agreement", 3),
        ("bagi hasil", 2), ("kemitraan", 2), ("kerja sama", 2),
        (r"\bpks\b", 3),
    ]),
]


def _extract_title_block(full_text: str) -> str:
    """Extract the leading document title/header before recitals or party blocks."""
    if not full_text:
        return ""
    title_lines: list[str] = []
    for raw_line in full_text[:2500].splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if title_lines and _TITLE_STOP_RE.match(line):
            break
        title_lines.append(line)
        if len(title_lines) >= 10:
            break
    return "\n".join(title_lines)[:800]


def _score_archetype_keywords(text: str) -> str | None:
    """Return the highest-scoring archetype from keyword/regex matches, or None."""
    sample = (text or "").strip().lower()
    if not sample:
        return None
    best_arch: str | None = None
    best_score = 0
    for arch, patterns in _ARCHETYPE_KEYWORDS:
        score = 0
        for pat, weight in patterns:
            matched = bool(re.search(pat, sample)) if pat.startswith(r"\b") else (pat in sample)
            if matched and weight > score:
                score = weight
        if score > best_score:
            best_score = score
            best_arch = arch
    return best_arch


def normalize_archetype(
    raw: str | None,
    fallback_text: str = "",
    doc_type: str = "",
    filename: str = "",
) -> str:
    """Determine contract archetype using title/filename keywords first, then LLM fallback."""
    # Tier 1: Document title block + filename (most reliable signal)
    title_block = _extract_title_block(fallback_text)
    clean_fname = re.sub(r"[_\-.]+", " ", filename or "")
    tier1_match = _score_archetype_keywords(f"{clean_fname}\n{title_block}")
    if tier1_match:
        return tier1_match

    # Tier 2: Extracted document type label (jenis_dokumen)
    tier2_match = _score_archetype_keywords(doc_type)
    if tier2_match:
        return tier2_match

    # Tier 3: Explicit LLM / caller archetype value
    val = str(raw or "").strip().upper()
    if val == "PARTNERSHIP":
        return "PKS"
    if val in ARCHETYPES and val != "GENERAL":
        return val

    alias_match = _score_archetype_keywords(val)
    if alias_match:
        return alias_match

    # Tier 4: Preamble / early body fallback when caller gave GENERAL or unknown
    tier4_match = _score_archetype_keywords((fallback_text or "")[:4500])
    if tier4_match:
        return tier4_match

    return "GENERAL"


def _looks_bilingual_or_english(full_text: str) -> bool:
    sample = (full_text or "")[:8000].lower()
    if not sample:
        return False
    en_markers = ("whereas", "now, therefore", "hereinafter", "governing law", "confidential information", "agreement")
    hits = sum(1 for m in en_markers if m in sample)
    return hits >= 2


def _find_candidate_clauses(rule: dict[str, Any], pasal_items: list[dict], top_k: int = 3) -> list[dict[str, str]]:
    """Score extracted clauses against a rule's Indonesian/English probes."""
    probes = [p.lower() for p in rule.get("semantic_probes", [])]
    scored: list[tuple[int, dict[str, str]]] = []
    for item in pasal_items:
        label = str(item.get("pasal") or "").strip()
        desc = str(item.get("deskripsi") or item.get("isi_pasal") or "").strip()
        if not desc:
            continue
        haystack = f"{label}\n{desc}".lower()
        score = sum(1 for p in probes if p in haystack)
        if score > 0:
            scored.append((score, {"pasal": label or "Klausul", "excerpt": desc[:900]}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in scored[:top_k]]


def _applicable_rules(contract_type: str, is_bilingual: bool, full_text: str) -> list[dict[str, Any]]:
    bilingual_or_en = is_bilingual or _looks_bilingual_or_english(full_text)
    rules: list[dict[str, Any]] = []
    for rule in PLAYBOOK_RULES:
        if contract_type not in rule["applicable_archetypes"]:
            continue
        if rule.get("requires_bilingual_or_foreign") and not bilingual_or_en:
            continue
        rules.append(rule)
    return rules


def _build_gap_record(
    rule: dict[str, Any],
    detection_state: str,
    why_dangerous: str | None = None,
    advisory_guidance: str | None = None,
    recommended_elements: list[str] | None = None,
    suggested_insertion_anchor: str | None = None,
    evidence_excerpt: str | None = None,
    existing_by_id: dict[str, dict] | None = None,
) -> dict[str, Any]:
    gap_id = rule["gap_id"]
    prev = (existing_by_id or {}).get(gap_id, {})
    resolution_status = prev.get("resolution_status")
    if resolution_status not in ("open", "addressed", "dismissed"):
        resolution_status = "open"

    return {
        "gap_id": gap_id,
        "clause_key": rule["clause_key"],
        "clause_title": rule["clause_title"],
        "clause_title_id": rule["clause_title_id"],
        "category": rule["category"],
        "severity": rule["severity"],
        "detection_state": detection_state,
        "resolution_status": resolution_status,
        "why_dangerous": (why_dangerous or rule["default_why_dangerous"]).strip(),
        "advisory_guidance": (advisory_guidance or rule["default_advisory_guidance"]).strip(),
        "recommended_elements": (
            [str(x).strip() for x in recommended_elements if str(x).strip()][:5]
            if isinstance(recommended_elements, list) and recommended_elements
            else list(rule["recommended_elements"])
        ),
        "suggested_insertion_anchor": (suggested_insertion_anchor or rule["default_anchor"]).strip(),
        "evidence_excerpt": evidence_excerpt.strip()[:500] if evidence_excerpt else None,
        "dismissed_reason": prev.get("dismissed_reason"),
    }


def detect_missing_clauses(
    pasal_items: list[dict],
    contract_type: str,
    doc_type: str = "",
    pihak_kita: str = "",
    peran_kita: str | None = None,
    is_bilingual: bool = False,
    full_text: str = "",
    existing_gaps: list[dict] | None = None,
    filename: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Run Reverse-RAG gap detection in one batched LLM call.

    Returns ``(normalized_contract_type, missing_clauses_list)``.
    """
    archetype = normalize_archetype(
        contract_type,
        fallback_text=full_text,
        doc_type=doc_type,
        filename=filename,
    )
    rules = _applicable_rules(archetype, is_bilingual, full_text)
    if not rules:
        return archetype, []

    existing_by_id = {
        str(g.get("gap_id")): g
        for g in (existing_gaps or [])
        if isinstance(g, dict) and g.get("gap_id")
    }

    candidates_by_key: dict[str, list[dict[str, str]]] = {}
    rules_block_lines: list[str] = []
    for rule in rules:
        cands = _find_candidate_clauses(rule, pasal_items, top_k=3)
        candidates_by_key[rule["clause_key"]] = cands
        if cands:
            cand_text = "\n".join(f"  - [{c['pasal']}]: {c['excerpt']}" for c in cands)
        else:
            cand_text = "  - TIDAK_DITEMUKAN_KLAUSUL_TERKAIT"
        rules_block_lines.append(
            f"ATURAN `{rule['clause_key']}` ({rule['clause_title_id']} | {rule['category']} | {rule['severity']}):\n"
            f"  Unsur wajib: {'; '.join(rule['recommended_elements'])}\n"
            f"  Potongan klausul kandidat dari dokumen:\n{cand_text}"
        )

    clause_outline = ", ".join(
        str(item.get("pasal") or "Klausul") for item in pasal_items[:25]
    ) or "Tidak teridentifikasi"

    party_desc = pihak_kita or "Pihak Kita"
    if peran_kita:
        party_desc = f"{party_desc} (peran: {peran_kita})"

    prompt = (
        "Anda adalah penasihat hukum korporasi senior Indonesia.\n"
        f"Analisis dokumen kontrak berikut (tipe terdeteksi: {archetype}, jenis dokumen: {doc_type or 'Kontrak'}) "
        f"dari sudut pandang: {party_desc}.\n"
        f"Daftar judul/pasal yang sudah ada dalam dokumen: {clause_outline}\n\n"
        "Untuk SETIAP aturan di bawah ini, periksa potongan klausul kandidat dan klasifikasikan menjadi:\n"
        '- "PRESENT": Kontrak sudah mengatur unsur-unsur pokoknya secara memadai dan konkret.\n'
        '- "VAGUE_INCOMPLETE": Disinggung secara umum tetapi belum memiliki angka, tenggat waktu, atau mekanisme pelaksanaan yang jelas.\n'
        '- "MISSING": Sama sekali belum diatur dalam dokumen.\n\n'
        "Untuk setiap aturan yang berstatus VAGUE_INCOMPLETE atau MISSING, tuliskan dalam BAHASA INDONESIA yang formal, "
        "konkret, dan spesifik terhadap transaksi dokumen ini:\n"
        "- `why_dangerous`: Jelaskan risiko hukum/komersial konkret bagi pihak kita beserta dasar hukum Indonesia yang relevan (misal KUHPerdata, UU PDP No. 27/2022, UU No. 24/2009, UU No. 30/1999).\n"
        "- `advisory_guidance`: Rekomendasi praktis langkah perbaikan yang harus dimasukkan sebelum penandatanganan.\n"
        "- `recommended_elements`: 3 poin ringkas unsur yang wajib ditambahkan.\n"
        "- `suggested_insertion_anchor`: Lokasi penempatan yang disarankan merujuk pasal yang ada di atas.\n"
        "- `evidence_excerpt`: Kutipan singkat klausul yang masih kabur (khusus VAGUE_INCOMPLETE, selain itu null).\n\n"
        + "\n\n".join(rules_block_lines)
        + "\n\nKembalikan HANYA JSON yang valid (tanpa markdown fence):\n"
        '{\n'
        '  "assessments": [\n'
        '    {\n'
        '      "clause_key": "margin_revenue_split",\n'
        '      "state": "PRESENT" | "VAGUE_INCOMPLETE" | "MISSING",\n'
        '      "why_dangerous": "...",\n'
        '      "advisory_guidance": "...",\n'
        '      "recommended_elements": ["...", "...", "..."],\n'
        '      "suggested_insertion_anchor": "Setelah Pasal ...",\n'
        '      "evidence_excerpt": null\n'
        '    }\n'
        '  ]\n'
        '}'
    )

    assessments_by_key: dict[str, dict[str, Any]] = {}
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.1, timeout=75)
        parsed = extract_json(raw)
        items = parsed.get("assessments") if isinstance(parsed, dict) else None
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict) and entry.get("clause_key"):
                    assessments_by_key[str(entry["clause_key"])] = entry
    except Exception as exc:
        print(f"[GapDetector] Batched LLM adjudication failed, using lexical fallback: {exc}")

    gaps: list[dict[str, Any]] = []
    for rule in rules:
        key = rule["clause_key"]
        item = assessments_by_key.get(key)
        cands = candidates_by_key.get(key, [])

        if item:
            state_raw = str(item.get("state") or "MISSING").strip().upper()
            if "PRESENT" in state_raw:
                continue
            detection_state = "vague_incomplete" if "VAGUE" in state_raw or "INCOMPLETE" in state_raw else "missing"
            evidence = item.get("evidence_excerpt")
            if detection_state == "vague_incomplete" and not evidence and cands:
                evidence = cands[0]["excerpt"][:300]
            gaps.append(
                _build_gap_record(
                    rule=rule,
                    detection_state=detection_state,
                    why_dangerous=item.get("why_dangerous"),
                    advisory_guidance=item.get("advisory_guidance"),
                    recommended_elements=item.get("recommended_elements"),
                    suggested_insertion_anchor=item.get("suggested_insertion_anchor"),
                    evidence_excerpt=evidence if detection_state == "vague_incomplete" else None,
                    existing_by_id=existing_by_id,
                )
            )
        else:
            if not cands:
                gaps.append(_build_gap_record(rule=rule, detection_state="missing", existing_by_id=existing_by_id))

    return archetype, gaps


def reconstruct_pasal_items_from_html(content_html: str) -> list[dict[str, str]]:
    """Split editor HTML into clause-like chunks for recheck-gaps."""
    if not content_html:
        return []
    text = re.sub(r"<br\s*/?>", "\n", content_html, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|h[1-6]|li|tr|blockquote)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    items: list[dict[str, str]] = []
    current_label = "Pembukaan"
    current_buf: list[str] = []
    heading_re = re.compile(r"^(?:pasal|article|bab|lampiran|annex|ketentuan)\b.*", re.IGNORECASE)

    for ln in lines:
        if heading_re.match(ln) and len(ln) <= 100:
            if current_buf:
                items.append({"pasal": current_label, "deskripsi": "\n".join(current_buf)[:4000]})
            current_label = ln
            current_buf = [ln]
        else:
            current_buf.append(ln)
    if current_buf:
        items.append({"pasal": current_label, "deskripsi": "\n".join(current_buf)[:4000]})
    return items
