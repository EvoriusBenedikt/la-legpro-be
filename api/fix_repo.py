import re

FILE_PATH = r"c:\Users\ben\Documents\Programming\Lintasarta\self-dev\la-legpro\la-legpro-be\api\routers\repository.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

correct_function = """def ingest_document_background(file_path: str, doc_id: str, filename: str, nomor: str, jenis: str, sektor: str, status: str, klasifikasi: str):
    try:
        from pdf_parser import LegalDocumentParser, LegalChunker
        parser = LegalDocumentParser()
        chunker = LegalChunker()
        
        full_text = parser.parse_pdf(file_path)
        
        # Duplicate Detection (FR-5) 
        fingerprint_text = full_text[:1500]
        from main import get_chroma_collection
        collection = get_chroma_collection()
        dup_results = collection.query(
            query_texts=[fingerprint_text],
            n_results=1
        )
        
        import sqlite3
        import os
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        db_path = os.path.join(BASE_DIR, "data", "legal_metadata.db")
        conn = sqlite3.connect(db_path, timeout=30.0)
        c = conn.cursor()
        
        if dup_results and dup_results['distances'] and len(dup_results['distances'][0]) > 0:
            dist = dup_results['distances'][0][0]
            if dist < 0.15:
                # Cleanup temp file
                if os.path.exists(file_path):
                    os.remove(file_path)
                c.execute("UPDATE regulations SET status = 'Gagal - Duplikat' WHERE id = ?", (doc_id,))
                conn.commit()
                conn.close()
                return

        print(f"File saved to DB. Now parsing and embedding: {filename}")
        
        # Contextual Enrichment - Generate Global Document Summary
        document_summary = ""
        try:
            from main import call_glm
            summary_prompt = (
                "Buatlah ringkasan singkat (maksimal 2 kalimat) yang menjelaskan tentang apa dokumen ini, "
                "siapa pihak yang terlibat, dan apa topik utamanya. "
                "Tujuan ringkasan ini adalah untuk memberikan konteks global pada potongan-potongan kecil teks dokumen.\\n\\n"
                f"TEKS DOKUMEN (Bagian Awal):\\n{full_text[:4000]}"
            )
            document_summary = call_glm([{"role": "user", "content": summary_prompt}], temperature=0.1, timeout=30)
            print(f"Generated contextual summary: {document_summary}")
        except Exception as e:
            print(f"Warning: Failed to generate document summary: {e}")

        # Vector DB Injection
        base_metadata = {
            "reg_id": doc_id,
            "judul": filename.replace('.pdf', ''),
            "nomor": nomor,
            "jenis": jenis,
            "sektor": sektor,
            "status": status
        }
        
        chunks = chunker.chunk_document(full_text, base_metadata, document_summary=document_summary)
        
        documents = []
        metadatas = []
        ids = []
        
        import hashlib
        for i, c_data in enumerate(chunks):
            clean_metadata = {k: v for k, v in c_data["metadata"].items() if v is not None}
            chunk_id = f"{nomor}_chunk_{i}"
            hash_id = hashlib.md5(chunk_id.encode('utf-8')).hexdigest()
            
            documents.append(c_data["text"])
            metadatas.append(clean_metadata)
            ids.append(hash_id)
            
        if documents:
            collection = get_chroma_collection()
            collection.add(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )
            print(f"Successfully added {len(documents)} chunks to ChromaDB!")
        
        c.execute("UPDATE regulations SET status = 'Berlaku' WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()

        # Knowledge Graph Extraction (real-time, FR-KG)
        judul = filename.replace('.pdf', '')
        try:
            from main import extract_and_store_graph
            extract_and_store_graph(doc_id, full_text, nomor, judul, jenis)
        except Exception as kg_err:
            print(f"[KG] Non-fatal extraction error for {nomor}: {kg_err}")
        return
        
    except Exception as e:
        print(f"Error processing PDF: {e}")
        try:
            import sqlite3
            import os
            BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            conn = sqlite3.connect(os.path.join(BASE_DIR, "data", "legal_metadata.db"), timeout=30.0)
            c = conn.cursor()
            c.execute("UPDATE regulations SET status = 'Gagal - Error' WHERE id = ?", (doc_id,))
            conn.commit()
            conn.close()
        except:
            pass"""

# regex to replace from def ingest_document_background to the next # or EOF
pattern = r"def ingest_document_background.*?except:\s+pass"
new_content = re.sub(pattern, correct_function, content, flags=re.DOTALL)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(new_content)
    
print("Successfully fixed repository.py!")
