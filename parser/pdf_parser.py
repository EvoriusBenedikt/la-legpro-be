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
        # We split by 'BAB' followed by Roman numerals OR 'Pasal' followed by numbers
        # The positive lookahead (?=...) keeps the delimiter in the chunk
        self.split_pattern = re.compile(r'(?=\b(?:BAB\s+[IVXLCDM]+|Pasal\s+\d+)\b)', flags=re.IGNORECASE | re.MULTILINE)

    def chunk_document(self, text, doc_metadata=None):
        """
        Splits the document logically by BAB or Pasal.
        """
        raw_chunks = self.split_pattern.split(text)
        
        chunks = []
        current_chunk = ""
        
        for c in raw_chunks:
            c_strip = c.strip()
            if not c_strip:
                continue
                
            # If the chunk is too small, just append to current (it might be a false positive or just a header)
            if len(c_strip) < 50 and not re.search(r'\b(?:BAB\s+|Pasal\s+)', c_strip, re.IGNORECASE):
                current_chunk += " " + c_strip
            else:
                if current_chunk:
                    chunks.append(self._clean_chunk(current_chunk, doc_metadata))
                current_chunk = c_strip
                
        if current_chunk:
            chunks.append(self._clean_chunk(current_chunk, doc_metadata))
            
        return chunks
        
    def _clean_chunk(self, chunk_text, metadata):
        """Clean up the chunk and attach metadata."""
        # Clean extra spaces
        clean_text = re.sub(r'\s+', ' ', chunk_text).strip()
        
        return {
            "text": clean_text,
            "metadata": metadata or {}
        }

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
