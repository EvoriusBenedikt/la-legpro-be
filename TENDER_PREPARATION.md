# Dokumen Persiapan Tender: Lintasarta Legal AI (Compliance & Analyzer)

Dokumen ini disusun untuk memberikan gambaran komprehensif mengenai produk Lintasarta Legal AI guna keperluan presentasi, _pitching_, atau persiapan tender.

---

## 1. Ringkasan Eksekutif (Executive Summary)
**Lintasarta Legal AI** adalah platform kecerdasan buatan (AI) terintegrasi yang dirancang khusus untuk memodernisasi alur kerja departemen legal. Sistem ini mampu melakukan ekstraksi, analisis kepatuhan (compliance), pemantauan kontrak, serta penyusunan draf dokumen hukum (PKS, NDA, dll) secara otomatis dengan rujukan langsung ke basis pengetahuan hukum Indonesia (OJK, UU ITE, UU PDP, KUH Perdata).

## 2. Fitur Utama & Cara Kerja (How the Product Works)

### A. Automated Compliance Checker (Audit Kepatuhan Otomatis)
- **Cara Kerja:** Pengguna mengunggah dokumen (PDF/Hasil Scan). Sistem mengekstrak teks menggunakan teknologi OCR hybrid. Melalui arsitektur AI *Multi-Pass*, sistem membedah dokumen ke dalam entitas (Pihak 1/2, Sektor), durasi kontrak, dan memecah setiap pasal. Pasal-pasal tersebut kemudian di-*cross-check* dengan regulasi pemerintah menggunakan Retrieval-Augmented Generation (RAG) (ChromaDB).
- **Output:** Laporan status tiap pasal (Sesuai, Beresiko, Fatal, Tidak Diatur) lengkap dengan pasal regulasi pembanding dan rekomendasi perbaikan hukum.

### B. Smart Contract Monitor (Pemantauan Masa Berlaku)
- **Cara Kerja:** AI secara proaktif mengekstrak `tanggal_pembuatan` dan `durasi_perjanjian` (maupun tanggal kedaluwarsa eksplisit). Data ini divisualisasikan dalam *dashboard* yang mengkategorikan kontrak menjadi: **Aktif**, **Segera Berakhir** (dalam 31 hari), dan **Kedaluwarsa**.

### C. AI Legal Repository & Drafter
- **Cara Kerja:** Chatbot cerdas dan generator dokumen yang terhubung langsung ke basis data hukum (OJK, Keuangan, Ketenagakerjaan). Pengguna dapat memberikan *prompt* (contoh: "Buat draf NDA untuk vendor IT"), dan sistem akan merangkai dokumen yang sesuai standar regulasi terkini. Dapat di-ekspor langsung ke format Microsoft Word (`.doc`).

---

## 3. Keunggulan Kompetitif (Pros & Unique Value Proposition)

1. **Akurasi Ekstraksi Tinggi (Hybrid Engine):** Tidak hanya bergantung pada LLM yang rentan halusinasi, sistem ini menggunakan arsitektur *Multi-Pass* (Pemisahan tugas AI: satu untuk metadata, satu untuk durasi, satu untuk analisis) dikombinasikan dengan _Regex fallback_ untuk memastikan tidak ada tanggal atau angka yang meleset.
2. **Kategorisasi *Knowledge Base* Berlapis:** Basis data vektor (ChromaDB) mengelompokkan aturan berdasarkan jenis dokumen (contoh: aturan NDA tidak akan tumpang tindih dengan aturan PKS Perbankan), namun membagikan aturan dasar (KUH Perdata) lintas sektor secara cerdas.
3. **Optimasi Waktu (Time & Cost Efficiency):** Pekerjaan _review_ kontrak setebal puluhan halaman yang biasa memakan waktu hitungan hari oleh konsultan hukum dapat diselesaikan dalam waktu 2-4 menit.
4. **Privasi & Keamanan Data:** Semua basis data hukum dan *vector storage* di-host secara lokal di arsitektur perusahaan, mencegah bocornya rahasia dagang ke platform AI publik.

---

## 4. Keterbatasan Saat Ini (Cons)

1. **Ketergantungan pada Kualitas *Scan*:** Untuk dokumen non-digital (kertas yang di-scan), akurasi analisis sangat bergantung pada kejelasan hasil _scan_. OCR bisa gagal membaca teks yang terlalu buram atau berstempel tebal.
2. ***Not a Human Replacement*:** Sistem ini bersifat sebagai *co-pilot* (asisten cerdas). Hasil analisis tingkat tinggi tetap membutuhkan stempel akhir (final review) dari *Legal Counsel* manusia, terutama untuk penafsiran hukum yang sangat ambigu.
3. **Pembaruan Regulasi Manual:** Walaupun sistem canggih, basis data vektor (ChromaDB) harus di-ingest/diperbarui secara berkala jika ada Undang-Undang atau aturan OJK baru yang disahkan pemerintah.

---

## 5. Tantangan Implementasi (Challenges & Mitigations)

| Tantangan | Strategi Mitigasi yang Telah Diterapkan |
| :--- | :--- |
| **Halusinasi LLM (AI mengarang aturan)** | Menggunakan sistem RAG (Retrieval-Augmented Generation) yang ketat; AI dilarang menjawab di luar konteks regulasi yang ditarik dari *Vector Database*. |
| **Batas Token AI (Dokumen sangat panjang)** | AI tidak membaca 100 halaman sekaligus. Sistem memecah dokumen menjadi *chunks* atau per-pasal, dan mengirimkannya secara paralel ke model AI. |
| **Dokumen Tidak Standar** | Ada sistem *fallback*. Jika format pasal tidak terbaca, sistem beralih mengekstrak durasi melalui pencarian pola kalimat eksplisit (*regex*). |

---

## 6. Target Pasar (Use Cases)
- **Perbankan & Fintech:** Mengaudit PKS dengan vendor atau nasabah terhadap peraturan OJK.
- **Perusahaan BUMN & Korporasi Besar:** Menyusun NDA massal dan melacak kapan kontrak dengan _supplier_ perlu diperpanjang.
- **Firma Hukum (Law Firms):** Akselerasi proses *Due Diligence* dan riset dokumen hukum historis.
