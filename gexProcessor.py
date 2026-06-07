import io
import gc
import os
from gitalertmanager import AlertManager
from playwright.sync_api import sync_playwright


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
            # headless=True is default, but you can explicitly write it
            browser = p.chromium.launch(headless=True) 
            page = browser.new_page()
            page.goto("https://ghost-gex.streamlit.app/~/+/", wait_until="networkidle")
            connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))
            connection_locator.wait_for(state="visible", timeout=15000)
            #print(page.content())
            page.locator('button[data-testid="stTab"][role="tab"]').filter(has_text="Overview Metrics").click()
            connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))
            connection_locator.wait_for(state="visible", timeout=15000)

            slider = page.get_by_role("slider", name="Strike Range ±")
            slider.focus()

            # Get the current value from the HTML attribute
            current_value = int(slider.get_attribute("aria-valuenow"))

            # Press ArrowLeft until the value reaches 20
            while current_value > 14:
                slider.press("ArrowLeft")
                current_value -= 1

            # Press ArrowRight if the initial value happened to be lower than 20
            while current_value < 14:
                slider.press("ArrowRight")
                current_value += 1

            connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))
            connection_locator.wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(300) 

            tags_locator = page.locator('span[data-baseweb="tag"][role="button"]')
            tag_count = tags_locator.count()

            # If there is more than 1 tag, delete everything except the first one (index 0)
            if tag_count > 1:
                # Loop backward from the last index down to index 1
                for i in range(tag_count - 1, 0, -1):
                    # Target the specific tag's inner Delete SVG icon and click it
                    tags_locator.nth(i).locator('svg[title="Delete"]').click()
                    connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))
                    connection_locator.wait_for(state="visible", timeout=15000)
                    page.wait_for_timeout(300) 
            
            checkbox = page.get_by_label("Enable AI Analysis")
            is_checked = checkbox.evaluate("el => el.checked || el.getAttribute('aria-checked') === 'true'")
            if is_checked:
                checkbox.evaluate("el => el.click()")
                
            connection_locator = page.locator('[data-test-connection-state="CONNECTED"]').and_(page.locator('[data-test-script-state="notRunning"]'))
            connection_locator.wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(300) 

            svg_element = page.locator('[data-testid="stPlotlyChart"]').first.locator('.user-select-none.svg-container').first
            svg_element.screenshot(path="streamlit_chart.png")
            png_bytes = svg_element.screenshot()
            buf.write(png_bytes)
            buf.seek(0)

            # Loop through each element and print its inner HTML
            # for index, element in enumerate(elements_list):
            #     svg_element = element.locator('[class="user-select-none svg-container"]').first
            #     print(f"--- Chart Element {index + 1} Inner HTML ---")
            #     print(element.inner_html())
            #     print("\n" + "="*50 + "\n")
            #     svg_element.screenshot(path="streamlit_chart"+str(index)+".png")

            browser.close()
            gc.collect()
        return buf
        
        
    def processrequest(self) -> io.BytesIO:
        """Convenience wrapper matching the SectorPerformance interface."""
        return self.capture_chart()