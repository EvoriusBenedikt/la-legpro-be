import os
import fitz  # PyMuPDF
import re

# ── Disable Intel MKL-DNN BEFORE paddle is imported (must be first) ──
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["OMP_NUM_THREADS"] = "1"

from paddleocr import PaddleOCR
from PIL import Image
import numpy as np

# Suppress debug logs from PaddleOCR if possible
import logging
logging.getLogger("ppocr").setLevel(logging.WARNING)

class LegalDocumentParser:
    def __init__(self, tesseract_fallback=False):
        print("Initializing PaddleOCR for fallback... (This may take a moment to download weights on first run)")
        try:
            self.ocr = PaddleOCR(
                use_angle_cls=False,   # skip the angle-classifier — that's what crashes with MKL
                lang='id',
                show_log=False,
            )
        except Exception as e:
            print(f"Failed to initialize PaddleOCR: {e}. Scanned pages will be skipped.")
            self.ocr = None
        
    def parse_pdf(self, pdf_path, force_ocr: bool = False):
        """
        Parses a PDF using PyMuPDF. If a page contains very little text (scanned),
        it falls back to PaddleOCR. If force_ocr=True, OCR is used for every page.
        """
        doc = fitz.open(pdf_path)
        full_text = ""
        
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            text = page.get_text("text").strip()
            
            # Threshold for fallback (e.g. less than 100 characters on a page)
            if force_ocr or len(text) < 100:
                print(f"  [OCR Fallback] Page {page_num + 1} has insufficient text. Running PaddleOCR...")
                text = self._ocr_page(page)
            else:
                # Basic cleanup of digital PDF texts (remove excessive newlines but keep paragraph breaks)
                text = re.sub(r'([^\n])\n([^\n])', r'\1 \2', text) # merge lines that don't have blank lines between
                
            full_text += text + "\n\n"
            
        doc.close()
        return full_text
        
    def _ocr_page(self, page):
        """Extract text from a PyMuPDF page. Tries PaddleOCR first; falls back to easyocr."""
        # Render page to image at 200 DPI
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, pix.n))
        if pix.n == 4:
            img_array = np.array(Image.fromarray(img_array, mode="RGBA").convert("RGB"))

        # ── Attempt 1: PaddleOCR ──
        if self.ocr:
            try:
                result = self.ocr.ocr(img_array, cls=False)
                page_text = []
                if result and result[0]:
                    for line in result[0]:
                        page_text.append(line[1][0])
                if page_text:
                    return " ".join(page_text)
            except Exception as paddle_err:
                print(f"  [PaddleOCR failed: {type(paddle_err).__name__}] Falling back to easyocr...")

        # ── Attempt 2: easyocr (no MKL dependency) ──
        try:
            import easyocr
            if not hasattr(self, "_easyocr"):
                print("  [Init easyocr — loading id+en model on first use...]")
                self._easyocr = easyocr.Reader(["id", "en"], gpu=False, verbose=False)
            result = self._easyocr.readtext(img_array, detail=0)
            if result:
                return " ".join(result)
        except Exception as easy_err:
            print(f"  [easyocr also failed: {easy_err}]")

        return "[HALAMAN BERUPA GAMBAR - OCR GAGAL]"


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

        # Precompute character lengths for fast window sliding
        lengths = [len(s) for s in sentences]
        
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

if __name__ == "__main__":
    import glob
    
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    PDF_DIR = os.path.join(BASE_DIR, "data", "pdfs")
    
    pdfs = glob.glob(os.path.join(PDF_DIR, "*.pdf"))
    
    if not pdfs:
        print("No PDFs found!")
        exit(1)
        
    parser = LegalDocumentParser()
    chunker = LegalChunker()
    
    test_pdf = pdfs[0]
    print(f"\nProcessing PDF: {os.path.basename(test_pdf)}")
    
    # 1. Parse
    full_text = parser.parse_pdf(test_pdf)
    print(f"Extracted {len(full_text)} characters.")
    
    # 2. Chunk
    metadata = {"filename": os.path.basename(test_pdf), "source": "OJK"}
    chunks = chunker.chunk_document(full_text, metadata)
    
    print(f"\nCreated {len(chunks)} logical chunks.")
    
    # Display sample chunks
    for i in range(min(5, len(chunks))):
        c = chunks[i]
        print(f"\n--- Chunk {i+1} ---")
        preview = c['text'][:200] + "..." if len(c['text']) > 200 else c['text']
        print(preview)
