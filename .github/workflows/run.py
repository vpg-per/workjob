# standard script structure
from playwright.sync_api import sync_playwright
import os
from gitalertmanager import AlertManager

with sync_playwright() as p:
    # CRITICAL: headless must be True for GitHub Actions to work
    browser = p.chromium.launch(headless=True) 
    page = browser.new_page()
    page.goto("https://example.com")
    
    alertMgr = AlertManager()
    alertMgr.send_chart_alert(page.title())
    print(page.title())
    browser.close()
