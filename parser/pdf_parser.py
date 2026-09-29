"""Legal-document chunking utilities.

(R1 bugfix 2026-09-28: this module also carried LegalDocumentParser, a
PyMuPDF + PaddleOCR/easyocr extractor. That chain was dead in every shipped
image -- the class was written against the PaddleOCR 2.x API (show_log /
use_angle_cls kwargs, .ocr()) while requirements pinned paddleocr 3.7.0, and
neither paddlepaddle nor easyocr was ever installed -- so every scanned page
silently became a "[HALAMAN BERUPA GAMBAR - OCR GAGAL]" placeholder. All PDF
text extraction now goes through services.rag_service.extract_text_hybrid
(PyMuPDF for digital pages, per-page VLM for scanned/garbled ones); see
la-legpro-doc/bug_reports.md. Only LegalChunker -- shared by every ingestion
path -- remains here.)
"""
import re


class LegalChunker:
    def __init__(self):
        # We will split text into sentences for fine-grained chunking
        self.sentence_pattern = re.compile(r'(?<=[.!?])\s+')

    def chunk_document(self, text, doc_metadata=None, document_summary=""):
        """
        Splits the document into small indexed chunks (approx 100 tokens / 500 chars)
        but attaches a large surrounding context window (approx 400 tokens / 2000 chars)
        to the metadata for retrieval. Also prepends the document summary to the indexed text.
        """
        # Split into raw sentences and filter empties
        sentences = [s.strip() for s in self.sentence_pattern.split(text) if s.strip()]
        
        chunks = []
        SMALL_CHUNK_TARGET = 500  # approx 100 tokens
        WINDOW_TARGET = 2000      # approx 400 tokens
        
        i = 0
        while i < len(sentences):
            # 1. Build the small indexed chunk
            small_chunk_text = ""
            small_chunk_sentences = 0
            while i + small_chunk_sentences < len(sentences) and len(small_chunk_text) < SMALL_CHUNK_TARGET:
                small_chunk_text += sentences[i + small_chunk_sentences] + " "
                small_chunk_sentences += 1
                
            if not small_chunk_text.strip():
                i += 1
                continue
                
            # 2. Build the surrounding window context
            # We want to grab sentences before and after to reach WINDOW_TARGET
            window_text = small_chunk_text
            left_idx = i - 1
            right_idx = i + small_chunk_sentences
            
            # Expand outwards until window size is reached
            while len(window_text) < WINDOW_TARGET and (left_idx >= 0 or right_idx < len(sentences)):
                if left_idx >= 0:
                    window_text = sentences[left_idx] + " " + window_text
                    left_idx -= 1
                if len(window_text) >= WINDOW_TARGET:
                    break
                if right_idx < len(sentences):
                    window_text = window_text + " " + sentences[right_idx]
                    right_idx += 1

            # 3. Apply Contextual Enrichment
            enriched_indexed_text = small_chunk_text.strip()
            if document_summary:
                enriched_indexed_text = f"RINGKASAN DOKUMEN: {document_summary}\n\nPOTONGAN TEKS: {enriched_indexed_text}"

            # 4. Save the chunk
            meta = doc_metadata.copy() if doc_metadata else {}
            meta["window_context"] = window_text.strip()
            
            chunks.append({
                "text": enriched_indexed_text,
                "metadata": meta
            })
            
            # Move forward by the small chunk size (this creates natural overlap 
            # in the window_context but unique indexed anchors)
            i += max(1, small_chunk_sentences)
            
        return chunks

    def _clean_chunk(self, chunk_text, metadata):
        # Kept for backward compatibility if needed, though unused in new flow
        clean_text = re.sub(r'\s+', ' ', chunk_text).strip()
        return {"text": clean_text, "metadata": metadata or {}}
