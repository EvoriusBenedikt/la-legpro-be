import requests
import urllib3
import psycopg
from lxml import html as lxml_html
import os
import sys
import time
import re

urllib3.disable_warnings()

# (Migration M4: rows go to the legpro PostgreSQL database -- the
# scraper.regulations schema/table migrated from ojk_metadata.db in M2. The
# legacy BeautifulSoup dependency was never in requirements.txt (the script
# could not run in the container at all); the two small parse jobs below use
# lxml, which is already a dependency.)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import script_env
script_env.bootstrap(needs_api=False)

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

OUTPUT_DIR = os.path.join(BASE_DIR, "data", "pdfs")

# Setup dirs
os.makedirs(OUTPUT_DIR, exist_ok=True)

def init_db():
    # The legacy CREATE TABLE IF NOT EXISTS bootstrap is gone: scraper.regulations
    # is owned by migrations/001_schema.sql. autocommit mirrors the legacy
    # per-statement sqlite3 behaviour.
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)

def get_detail_and_download(session, detail_url, output_path):
    """Hits the detail page, finds the 'Unduh' link, and downloads the PDF."""
    try:
        if not detail_url.startswith('http'):
            detail_url = BASE_URL + detail_url
            
        response = session.get(detail_url, verify=False, timeout=10)
        response.raise_for_status()
        # (Migration M4: lxml replaces BeautifulSoup -- same two-step search:
        # first an <a> whose visible text contains 'unduh', then any <a> whose
        # href contains 'DownloadDokumen'.)
        tree = lxml_html.fromstring(response.text)

        unduh_link = None
        for a in tree.xpath('//a'):
            if 'unduh' in (a.text_content() or '').lower():
                unduh_link = a
                break
        if unduh_link is None:
            hits = tree.xpath('//a[contains(@href, "DownloadDokumen")]')
            unduh_link = hits[0] if hits else None

        if unduh_link is not None and unduh_link.get('href'):
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
                    
                    # Parse the link and title (Migration M4: lxml; the aaData
                    # cell is an HTML fragment whose root may BE the <a> tag)
                    fragment = lxml_html.fromstring(html_col)
                    a_tag = fragment if fragment.tag == 'a' else fragment.find('.//a')
                    if a_tag is None:
                        continue

                    detail_url = a_tag.get('href')
                    judul = (a_tag.text_content() or '').strip()
                    
                    if not detail_url.startswith('http'):
                        full_detail_url = BASE_URL + detail_url
                    else:
                        full_detail_url = detail_url
                    
                    # Clean up number for filename
                    safe_nomor = re.sub(r'[^a-zA-Z0-9_\-]', '_', nomor)
                    filename = f"{jenis_name}_{sektor_name}_{safe_nomor}.pdf"
                    filepath = os.path.join(OUTPUT_DIR, filename)
                    
                    # Check if already in DB
                    c.execute('SELECT id FROM scraper.regulations WHERE detail_url = %s', (full_detail_url,))
                    if c.fetchone():
                        print(f"Skipping (already in DB): {nomor}")
                        continue
                        
                    print(f"Processing: {nomor} - {judul[:30]}...")
                    download_url, success = get_detail_and_download(session, full_detail_url, filepath)
                    
                    if success:
                        # ON CONFLICT guards the UNIQUE detail_url so one late
                        # duplicate cannot abort the whole sector loop (the
                        # connection is autocommit -- no conn.commit() needed).
                        c.execute('''
                            INSERT INTO scraper.regulations (judul, nomor, jenis, sektor, status, detail_url, download_url, local_path)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (detail_url) DO NOTHING
                        ''', (judul, nomor, jenis_name, sektor_name, status, full_detail_url, download_url, filepath))
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
