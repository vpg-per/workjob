import io
import gc
import os
import tempfile
import time
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

GEX_URL = "https://optionexp.streamlit.app/~/+/?symbol=SPY&min_oi=10&strikes=8/favicon.png"
VOLUME_HISTORY_URL = "https://optionexp.streamlit.app/~/+/Volume_History"
WAKEUP_URL = "https://optionexp.streamlit.app/"

WAKE_SETTLE_SECONDS = 99


class GexProcessor:
    def __init__(self):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

    def _wait_for_connection(self, page, timeout: int = 60000) -> bool:
        """Waits for the app to report CONNECTED + notRunning. Returns False on timeout."""
        connection_locator = page.locator(
            '[data-test-connection-state="CONNECTED"]'
        ).and_(page.locator('[data-test-script-state="notRunning"]'))
        try:
            connection_locator.wait_for(state="visible", timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            return False

    def _wake_app(self, page) -> None:
        """Visits the app root and clicks the wakeup button if the app is asleep."""
        page.goto(WAKEUP_URL, wait_until="load")
        page.wait_for_timeout(20000)
        sleep_button = page.locator('button[data-testid="wakeup-button-viewer"]')
        if sleep_button.is_visible():
            sleep_button.click()
        time.sleep(WAKE_SETTLE_SECONDS)

    def _download_chart(self, page) -> bytes:
        """Clicks the download button and returns the downloaded file's bytes."""
        download_button = page.locator('[data-testid="stDownloadButton"] button').first
        download_button.wait_for(state="visible", timeout=15000)
        with page.expect_download(timeout=20000) as download_info:
            download_button.click()
        download = download_info.value

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(
                tmp_dir, download.suggested_filename or "streamlit_chart.png"
            )
            download.save_as(tmp_path)
            with open(tmp_path, "rb") as f:
                return f.read()

    def _capture_chart_from_url(self, url: str) -> Optional[io.BytesIO]:
        """Navigate, ensure connection (waking the app if needed), download the chart.

        Returns a rewound BytesIO on success, or None if the capture failed. Callers
        must check for None rather than assuming they got usable image bytes.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="networkidle")

                if not self._wait_for_connection(page):
                    self._wake_app(page)
                    page.goto(url, wait_until="networkidle")
                    if not self._wait_for_connection(page):
                        print(f"Capture failed: app never connected for {url}")
                        return None

                data = self._download_chart(page)
                if not data:
                    print(f"Capture failed: empty download for {url}")
                    return None

                buf = io.BytesIO(data)
                buf.seek(0)
                return buf

            except PlaywrightTimeoutError as err:
                print(f"Capture failed for {url}: {err}")
                return None
            finally:
                browser.close()
                gc.collect()

    def capture_gexchart(self) -> Optional[io.BytesIO]:
        return self._capture_chart_from_url(GEX_URL)

    def capture_volumehistorychart(self) -> Optional[io.BytesIO]:
        return self._capture_chart_from_url(VOLUME_HISTORY_URL)

    def process_gexrequest(self) -> Optional[io.BytesIO]:
        return self.capture_gexchart()

    def process_volumehistoryrequest(self) -> Optional[io.BytesIO]:
        return self.capture_volumehistorychart()