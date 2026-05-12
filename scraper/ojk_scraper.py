import requests
from bs4 import BeautifulSoup
import urllib3
import json
import sqlite3
import os
import time
import re

urllib3.disable_warnings()

# Configuration
BASE_URL = "http://jdih.ojk.go.id"
DATA_URL = "http://jdih.ojk.go.id/Web/ViewPeraturan/ListDataPeraturan?sektor={sektor}&jenisPeraturan={jenis}&sLanguage="

SECTORS = {
    "01": "Perbankan",
    "02": "Pasar_Modal",
    "03": "IKNB",
    # Add more as needed
}

JENIS_PERATURAN = {
    "06": "POJK",
    "09": "SEOJK"
}

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "pdfs")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "ojk_metadata.db")

# Setup dirs
os.makedirs(OUTPUT_DIR, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS regulations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            judul TEXT,
            nomor TEXT,
            jenis TEXT,
            sektor TEXT,
            status TEXT,
            detail_url TEXT UNIQUE,
            download_url TEXT,
            local_path TEXT
        )
    ''')
    conn.commit()
    return conn

def get_detail_and_download(session, detail_url, output_path):
    """Hits the detail page, finds the 'Unduh' link, and downloads the PDF."""
    try:
        if not detail_url.startswith('http'):
            detail_url = BASE_URL + detail_url
            
        response = session.get(detail_url, verify=False, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        unduh_link = soup.find('a', string=lambda s: s and 'unduh' in s.lower())
        if not unduh_link:
            unduh_link = soup.find('a', href=lambda h: h and 'DownloadDokumen' in h)
            
        if unduh_link and unduh_link.get('href'):
            download_url = BASE_URL + unduh_link.get('href')
            
            # Download the actual file
            # print(f"Downloading PDF from {download_url}")
            pdf_resp = session.get(download_url, verify=False, timeout=20)
            content_type = pdf_resp.headers.get('Content-Type', '').lower()
            if pdf_resp.status_code == 200 and ('application/pdf' in content_type or 'application/octet-stream' in content_type):
                with open(output_path, 'wb') as f:
                    f.write(pdf_resp.content)
                return download_url, True
        return None, False
    except Exception as e:
        print(f"Error getting detail {detail_url}: {e}")
        return None, False

def scrape_ojk(limit_per_category=100):
    conn = init_db()
    c = conn.cursor()
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
        "X-Requested-With": "XMLHttpRequest"
    })
    
    for sektor_id, sektor_name in SECTORS.items():
        for jenis_id, jenis_name in JENIS_PERATURAN.items():
            print(f"\n--- Scraping {jenis_name} for {sektor_name} ---")
            target_url = DATA_URL.format(sektor=sektor_id, jenis=jenis_id)
            try:
                resp = session.get(target_url, verify=False, timeout=10)
                data = resp.json()
                if "aaData" not in data:
                    print("No aaData found in response.")
                    continue
                
                rows = data["aaData"]
                print(f"Found {len(rows)} records. Processing up to {limit_per_category} limits...")
                
                count = 0
                for row in rows:
                    if count >= limit_per_category:
                        break
                        
                    html_col = row[0]
                    nomor = str(row[1]).strip()
                    status = str(row[7]).strip()
                    
                    # Parse the link and title
                    soup = BeautifulSoup(html_col, 'html.parser')
                    a_tag = soup.find('a')
                    if not a_tag:
                        continue
                        
                    detail_url = a_tag.get('href')
                    judul = a_tag.text.strip()
                    
                    if not detail_url.startswith('http'):
                        full_detail_url = BASE_URL + detail_url
                    else:
                        full_detail_url = detail_url
                    
                    # Clean up number for filename
                    safe_nomor = re.sub(r'[^a-zA-Z0-9_\-]', '_', nomor)
                    filename = f"{jenis_name}_{sektor_name}_{safe_nomor}.pdf"
                    filepath = os.path.join(OUTPUT_DIR, filename)
                    
                    # Check if already in DB
                    c.execute('SELECT id FROM regulations WHERE detail_url = ?', (full_detail_url,))
                    if c.fetchone():
                        print(f"Skipping (already in DB): {nomor}")
                        continue
                        
                    print(f"Processing: {nomor} - {judul[:30]}...")
                    download_url, success = get_detail_and_download(session, full_detail_url, filepath)
                    
                    if success:
                        c.execute('''
                            INSERT INTO regulations (judul, nomor, jenis, sektor, status, detail_url, download_url, local_path)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (judul, nomor, jenis_name, sektor_name, status, full_detail_url, download_url, filepath))
                        conn.commit()
                        print("Saved PDF successfully.")
                    else:
                        print("Failed to download PDF.")
                        
                    count += 1
                    time.sleep(1) # Be polite to the server
                    
            except Exception as e:
                print(f"Error scraping sector {sektor_id}, jenis {jenis_id}: {e}")

    conn.close()

if __name__ == "__main__":
    print("Starting OJK Scraper Test Mode (fetching 2 items per category)...")
    scrape_ojk(limit_per_category=100)
    print("Done!")
