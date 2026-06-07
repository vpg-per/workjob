# standard script structure
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # CRITICAL: headless must be True for GitHub Actions to work
    browser = p.chromium.launch(headless=True) 
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
    browser.close()
