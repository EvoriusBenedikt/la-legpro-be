import os
import chromadb
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROMA_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

# Crucial NDA regulations
NDA_REGULATIONS = [
    {
        "judul": "Undang-Undang Nomor 30 Tahun 2000 tentang Rahasia Dagang",
        "nomor": "UU No. 30 Tahun 2000",
        "jenis": "Undang-Undang",
        "sektor": "Kekayaan Intelektual",
        "doc_category": "NDA",
        "pasals": [
            "Pasal 1: Rahasia Dagang adalah informasi yang tidak diketahui oleh umum di bidang teknologi dan/atau bisnis, mempunyai nilai ekonomi karena berguna dalam kegiatan usaha, dan dijaga kerahasiaannya oleh pemilik Rahasia Dagang.",
            "Pasal 2: Lingkup pelindungan Rahasia Dagang meliputi metode produksi, metode pengolahan, metode penjualan, atau informasi lain di bidang teknologi dan/atau bisnis yang memiliki nilai ekonomi dan tidak diketahui oleh masyarakat umum.",
            "Pasal 3: Rahasia Dagang mendapat pelindungan apabila informasi tersebut rahasia, mempunyai nilai ekonomi, dan dijaga kerahasiaannya melalui upaya sebagaimana mestinya.",
            "Pasal 11: Pemegang Hak Rahasia Dagang atau penerima Lisensi dapat menuntut siapa saja yang dengan sengaja dan tanpa hak menggunakan Rahasia Dagang miliknya.",
            "Pasal 13: Barangsiapa dengan sengaja dan tanpa hak menggunakan Rahasia Dagang pihak lain, dipidana dengan pidana penjara paling lama 2 (dua) tahun dan/atau denda paling banyak Rp300.000.000,00 (tiga ratus juta rupiah).",
            "Pasal 14: Pelanggaran Rahasia Dagang terjadi apabila seseorang dengan sengaja mengungkapkan Rahasia Dagang, mengingkari kesepakatan atau mengingkari kewajiban tertulis atau tidak tertulis untuk menjaga Rahasia Dagang."
        ]
    },
    {
        "judul": "Undang-Undang Nomor 27 Tahun 2022 tentang Pelindungan Data Pribadi",
        "nomor": "UU No. 27 Tahun 2022",
        "jenis": "Undang-Undang",
        "sektor": "Teknologi Informasi",
        "doc_category": "NDA",
        "pasals": [
            "Pasal 1: Data Pribadi adalah data tentang orang perseorangan yang teridentifikasi atau dapat diidentifikasi secara tersendiri atau dikombinasi dengan informasi lainnya.",
            "Pasal 20: Pengendali Data Pribadi wajib menjaga kerahasiaan Data Pribadi.",
            "Pasal 35: Pengendali Data Pribadi wajib melindungi dan memastikan keamanan Data Pribadi dengan melakukan pemrosesan Data Pribadi secara aman dan mencegah akses tidak sah.",
            "Pasal 65: Setiap Orang dilarang secara melawan hukum memperoleh atau mengumpulkan Data Pribadi yang bukan miliknya dengan maksud untuk menguntungkan diri sendiri atau orang lain.",
            "Pasal 67: Setiap Orang yang dengan sengaja dan melawan hukum memperoleh atau mengumpulkan Data Pribadi yang bukan miliknya, dipidana dengan pidana penjara paling lama 5 (lima) tahun dan/atau pidana denda paling banyak Rp5.000.000.000,00 (lima miliar rupiah)."
        ]
    },
    {
        "judul": "Undang-Undang Nomor 11 Tahun 2008 tentang Informasi dan Transaksi Elektronik",
        "nomor": "UU ITE",
        "jenis": "Undang-Undang",
        "sektor": "Teknologi Informasi",
        "doc_category": "NDA",
        "pasals": [
            "Pasal 30: Setiap Orang dengan sengaja dan tanpa hak atau melawan hukum mengakses Komputer dan/atau Sistem Elektronik milik Orang lain dengan cara apa pun.",
            "Pasal 32: Setiap Orang dengan sengaja dan tanpa hak atau melawan hukum dengan cara apa pun mengubah, menambah, mengurangi, melakukan transmisi, merusak, menghilangkan, memindahkan, menyembunyikan suatu Informasi Elektronik dan/atau Dokumen Elektronik milik Orang lain atau milik publik.",
            "Pasal 46: Setiap Orang yang memenuhi unsur sebagaimana dimaksud dalam Pasal 30 ayat (1) dipidana dengan pidana penjara paling lama 6 (enam) tahun dan/atau denda paling banyak Rp600.000.000,00 (enam ratus juta rupiah)."
        ]
    },
    {
        "judul": "Kitab Undang-Undang Hukum Perdata (KUHPerdata)",
        "nomor": "KUHPerdata",
        "jenis": "Undang-Undang",
        "sektor": "Hukum Perdata",
        "doc_category": "UMUM",
        "pasals": [
            "Pasal 1320: Untuk sahnya suatu perjanjian diperlukan empat syarat: sepakat mereka yang mengikatkan dirinya; kecakapan untuk membuat suatu perikatan; suatu hal tertentu; suatu sebab yang halal.",
            "Pasal 1338: Semua perjanjian yang dibuat secara sah berlaku sebagai undang-undang bagi mereka yang membuatnya. Perjanjian-perjanjian itu tidak dapat ditarik kembali selain dengan kesepakatan kedua belah pihak, atau karena alasan-alasan yang oleh undang-undang dinyatakan cukup untuk itu. Perjanjian harus dilaksanakan dengan itikad baik.",
            "Pasal 1339: Perjanjian-perjanjian tidak hanya mengikat untuk hal-hal yang dengan tegas dinyatakan di dalamnya, tetapi juga untuk segala sesuatu yang menurut sifat perjanjian, diharuskan oleh kepatutan, kebiasaan atau undang-undang.",
            "Pasal 1243: Penggantian biaya, kerugian dan bunga karena tak dipenuhinya suatu perikatan mulai diwajibkan, bila debitur, walaupun telah dinyatakan lalai, tetap lalai untuk memenuhi perikatan itu, atau jika sesuatu yang harus diberikan atau dilakukannya hanya dapat diberikan atau dilakukannya dalam waktu yang melampaui waktu yang telah ditentukan."
        ]
    }
]

def main():
    print(f"Connecting to ChromaDB at {CHROMA_DIR}...")
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    col = client.get_or_create_collection(name="ojk_regulations")
    
    added_count = 0
    for reg in NDA_REGULATIONS:
        print(f"Ingesting {reg['nomor']} ({reg['doc_category']})...")
        ids = []
        docs = []
        metas = []
        
        for i, pasal in enumerate(reg['pasals']):
            doc_id = f"mock_{reg['nomor'].replace(' ', '_')}_pasal_{i}"
            ids.append(doc_id)
            docs.append(pasal)
            metas.append({
                "reg_id": f"mock_{reg['nomor']}",
                "domain": "Indonesia",
                "judul": reg['judul'],
                "nomor": reg['nomor'],
                "jenis": reg['jenis'],
                "sektor": reg['sektor'],
                "status": "Berlaku",
                "doc_category": reg['doc_category'],
                "visibility": "public"
            })
            
        col.add(ids=ids, documents=docs, metadatas=metas)
        added_count += len(docs)
        
    print(f"Successfully ingested {added_count} crucial NDA/UMUM clauses into ChromaDB.")

if __name__ == "__main__":
    main()
