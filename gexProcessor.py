import io
import gc
import os
import tempfile
import time
from gitalertmanager import AlertManager
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

GEX_URL = "https://optionexp.streamlit.app/~/+/?symbol=SPY&min_oi=10&strikes=8/favicon.png"
VOLUME_HISTORY_URL = "https://optionexp.streamlit.app/~/+/Volume_History"
WAKEUP_URL = "https://optionexp.streamlit.app/"


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
        time.sleep(99)

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

    def _capture_chart_from_url(self, url: str) -> io.BytesIO:
        """Shared capture flow: navigate, ensure connection (waking the app if needed),
        download the chart, and return it as a buffer."""
        buf = io.BytesIO()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, wait_until="networkidle")
                is_connected = self._wait_for_connection(page)

                if not is_connected:
                    self._wake_app(page)
                    page.goto(url, wait_until="networkidle")
                    self._wait_for_connection(page)

                buf.write(self._download_chart(page))
                buf.seek(0)
                browser.close()

            except PlaywrightTimeoutError as err:
                print(f"Error: {err}")
                pass

            gc.collect()
        return buf

    def capture_gexchart(self) -> io.BytesIO:
        return self._capture_chart_from_url(GEX_URL)

    def capture_volumehistorychart(self) -> io.BytesIO:
        return self._capture_chart_from_url(VOLUME_HISTORY_URL)

    def process_gexrequest(self) -> io.BytesIO:
        return self.capture_gexchart()

    def process_volumehistoryrequest(self) -> io.BytesIO:
        return self.capture_volumehistorychart()