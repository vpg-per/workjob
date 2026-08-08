import io
import gc
import os
import tempfile
import time
from gitalertmanager import AlertManager
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

class GexProcessor:
    def __init__(self):
        
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
            
    def capture_chart(self) -> io.BytesIO:
        buf = io.BytesIO()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True) 
            page = browser.new_page()
            try:
                page.goto("https://soptionexp.streamlit.app/~/+/?symbol=SPY&min_oi=10&strikes=8/favicon.png", wait_until="networkidle")
                connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))

                is_connected = True
                try:
                    connection_locator.wait_for(state="visible", timeout=60000)
                except PlaywrightTimeoutError:
                    is_connected = False

                if is_connected == False:
                    page.goto("https://soptionexp.streamlit.app/", wait_until="load")
                    page.wait_for_timeout(20000)
                    sleep_button = page.locator('button[data-testid="wakeup-button-viewer"]')
                    if sleep_button.is_visible():
                        sleep_button.click()
                    time.sleep(99)
                    page.goto("https://soptionexp.streamlit.app/~/+/?symbol=SPY&min_oi=10&strikes=8/favicon.png", wait_until="networkidle")
                    connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))
                    connection_locator.wait_for(state="visible", timeout=60000)
                
                download_button = page.locator('[data-testid="stDownloadButton"] button').first
                download_button.wait_for(state="visible", timeout=15000)
                with page.expect_download(timeout=20000) as download_info:
                    download_button.click()
                download = download_info.value
    
                with tempfile.TemporaryDirectory() as tmp_dir:
                    tmp_path = os.path.join(tmp_dir, download.suggested_filename or "streamlit_chart.png")
                    download.save_as(tmp_path)
                    with open(tmp_path, "rb") as f:
                        buf.write(f.read())
                buf.seek(0)
                browser.close()
                
            except PlaywrightTimeoutError as err:
                print(f"Error: {err}")
                pass
                
            gc.collect()
        return buf
        
        
    def processrequest(self) -> io.BytesIO:
        """Convenience wrapper matching the SectorPerformance interface."""
        return self.capture_chart()