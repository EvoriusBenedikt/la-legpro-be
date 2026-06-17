import os

filepath = os.path.join("api", "main.py")
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update check_compliance signature and pass use_ocr to extract_text_multi_format
old_check = """@app.post("/api/check-compliance")
async def check_compliance(file: UploadFile = File(...)):"""
new_check = """@app.post("/api/check-compliance")
async def check_compliance(file: UploadFile = File(...), use_ocr: str = Form("false")):"""
content = content.replace(old_check, new_check)

old_extract_call = """        # ── Pass 0: Multi-Format Extraction ──────────────────────────────────
        print(f"[Compliance] Starting extraction for: {file.filename}")
        full_text = extract_text_multi_format(temp_path, file.filename)"""
new_extract_call = """        # ── Pass 0: Multi-Format Extraction ──────────────────────────────────
        print(f"[Compliance] Starting extraction for: {file.filename}")
        is_ocr = use_ocr.lower() == "true"
        full_text = extract_text_multi_format(temp_path, file.filename, use_ocr=is_ocr)"""
content = content.replace(old_extract_call, new_extract_call)

# 2. Update extract_text_multi_format
old_extract_def = """def extract_text_multi_format(file_path: str, filename: str) -> str:
    \"\"\"
    Extracts text from various file formats: PDF, DOCX, XLSX, PPTX, JPG, PNG, TXT.
    Routes to the appropriate extractor based on the file extension.
    \"\"\"
    ext = filename.lower().split('.')[-1]
    
    if ext == 'pdf':
        return extract_text_hybrid(file_path, force_vlm=False)
        
    elif ext == 'txt':
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
            
    elif ext in ['jpg', 'jpeg', 'png']:
        print(f"  [VLM] Image extraction for {filename}")
        with open(file_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')
        vlm_text = call_glm_vision(img_b64, VLM_PAGE_PROMPT, timeout=60)
        return vlm_text.strip()"""

new_extract_def = """def extract_text_multi_format(file_path: str, filename: str, use_ocr: bool = False) -> str:
    \"\"\"
    Extracts text from various file formats: PDF, DOCX, XLSX, PPTX, JPG, PNG, TXT.
    Routes to the appropriate extractor based on the file extension.
    \"\"\"
    ext = filename.lower().split('.')[-1]
    
    if ext == 'pdf':
        if use_ocr:
            print(f"  [OCR] Forcing traditional OCR extraction for {filename}")
            import sys
            if BASE_DIR not in sys.path:
                sys.path.append(BASE_DIR)
            from parser.pdf_parser import LegalDocumentParser
            parser = LegalDocumentParser()
            return parser.parse_pdf(file_path, force_ocr=True)
        else:
            return extract_text_hybrid(file_path, force_vlm=False)
        
    elif ext == 'txt':
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
            
    elif ext in ['jpg', 'jpeg', 'png']:
        if use_ocr:
            print(f"  [OCR] Traditional image extraction for {filename}")
            import sys
            if BASE_DIR not in sys.path:
                sys.path.append(BASE_DIR)
            from parser.pdf_parser import LegalDocumentParser
            parser = LegalDocumentParser()
            if parser.ocr:
                import numpy as np
                from PIL import Image
                img = Image.open(file_path).convert("RGB")
                img_array = np.array(img)
                result = parser.ocr.ocr(img_array, cls=False)
                page_text = []
                if result and result[0]:
                    for line in result[0]:
                        page_text.append(line[1][0])
                return " ".join(page_text)
            else:
                return "[OCR GAGAL INISIALISASI]"
        else:
            print(f"  [VLM] Image extraction for {filename}")
            with open(file_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode('utf-8')
            vlm_text = call_glm_vision(img_b64, VLM_PAGE_PROMPT, timeout=60)
            return vlm_text.strip()"""
content = content.replace(old_extract_def, new_extract_def)

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("Backend OCR patch applied successfully.")
