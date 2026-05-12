import os
import sys
import time
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Add parent directory to path to import base_scraper
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from base_scraper import BaseJDIHScraper

class KemnakerScraper(BaseJDIHScraper):
    def __init__(self):
        super().__init__("Kemnaker")
        self.base_url = "https://jdih.kemnaker.go.id"
        
    def setup_driver(self):
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        # Use webdriver_manager if needed, or assume chromedriver is in PATH/installed
        driver = webdriver.Chrome(options=options)
        return driver

    def scrape(self, limit: int = 100):
        print(f"--- Scraping {self.domain_name} (Target: {limit} items) ---")
        driver = self.setup_driver()
        try:
            # Go to the peraturan page
            driver.get(f"{self.base_url}/peraturan")
            
            # Wait for the dynamic list to load (up to 15 seconds)
            print("Waiting for page to load...")
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/peraturan/detail/']"))
                )
            except Exception as e:
                print("Timeout waiting for links to load. The site might be blocking headless browsers or using a different layout.")
            
            time.sleep(2) # Extra buffer for JS rendering
            
            # Find all regulation cards/links
            elements = driver.find_elements(By.CSS_SELECTOR, "a[href*='/peraturan/detail/']")
            urls = []
            for el in elements:
                href = el.get_attribute('href')
                if href and href not in urls:
                    urls.append(href)
                    
            print(f"Found {len(urls)} regulation links on the first page.")
            
            # If we need more, we'd have to handle pagination. 
            # For prototype, let's just grab what we can from the first load or loop pages.
            count = 0
            for detail_url in urls:
                if count >= limit:
                    break
                    
                if self.is_already_scraped(detail_url):
                    print(f"Skipping (already in DB): {detail_url}")
                    continue
                    
                print(f"Processing: {detail_url}")
                driver.get(detail_url)
                time.sleep(3) # wait for detail page
                
                # Extract metadata
                judul = driver.title
                nomor = "Unknown"
                
                # Find download button
                pdf_link = None
                try:
                    # Look for links containing .pdf, download, or btn-document
                    links = driver.find_elements(By.TAG_NAME, "a")
                    for link in links:
                        href = link.get_attribute("href")
                        text = link.text.lower()
                        # The user found: <a href="/download.php..." class="btn btn-document..."><span>...pdf</span></a>
                        if href and ('.pdf' in href.lower() or '.pdf' in text or 'unduh' in text or '/download' in href.lower()):
                            if not href.startswith('http'):
                                # Handle relative URLs like /download.php
                                pdf_link = self.base_url + (href if href.startswith('/') else '/' + href)
                            else:
                                pdf_link = href
                            break
                except Exception as e:
                    print(f"Error finding PDF link: {e}")
                
                if pdf_link:
                    # Clean filename
                    filename = self.clean_filename(judul[:50]) + ".pdf"
                    local_path = self.download_pdf(pdf_link, filename)
                    if local_path:
                        self.save_to_db(
                            judul=judul,
                            nomor=nomor,
                            jenis="Peraturan",
                            sektor="Ketenagakerjaan",
                            status="Berlaku",
                            detail_url=detail_url,
                            download_url=pdf_link,
                            local_path=local_path
                        )
                        print("Saved PDF successfully.")
                        count += 1
                else:
                    print("No PDF link found on detail page.")
                    
        finally:
            driver.quit()

if __name__ == "__main__":
    scraper = KemnakerScraper()
    scraper.scrape(limit=5)
