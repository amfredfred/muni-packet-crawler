"""
Scrapes the League of Arizona Cities and Towns' resource directory — a WP
Business Directory plugin listing that paginates client-side ("Next" link,
no discoverable API endpoint), so this drives headless Chromium through all
pages rather than parsing a single static response.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection
from scrapers._playwright_fetch import USER_AGENT
from playwright.sync_api import sync_playwright

URL = "https://www.azleague.org/resourcedirectory"

ENTRY_RE = re.compile(
    r'font-weight: bold;">(?:City|Town) of ([^<]+)</span>.*?Link: <a href="(https?://[^"]+)"',
    re.S,
)

MAX_PAGES = 20


def scrape_all() -> list[tuple[str, str]]:
    results: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(URL, timeout=60000, wait_until="networkidle")
        page.wait_for_timeout(1500)

        for _ in range(MAX_PAGES):
            html = page.content()
            for name, website in ENTRY_RE.findall(html):
                results[name.strip()] = website
            next_link = page.query_selector('a:has-text("Next")')
            if not next_link:
                break
            try:
                next_link.click()
            except Exception:
                break
            page.wait_for_timeout(1200)

        browser.close()
    return list(results.items())


def main() -> None:
    entries = scrape_all()

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for name, website in entries:
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Arizona', 'city', ?, 'az-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Arizona municipalities indexed")


if __name__ == "__main__":
    main()
