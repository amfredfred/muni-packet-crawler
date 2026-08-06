"""Shared helper: fetch a page's rendered HTML via headless Chromium, for
sites that block plain `requests` (bot-detection on non-browser clients)."""

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_rendered_html(url: str, wait_ms: int = 2000, timeout_ms: int = 30000) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, timeout=timeout_ms)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
        return html
