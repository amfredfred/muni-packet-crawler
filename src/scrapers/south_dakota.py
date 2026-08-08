"""
The South Dakota Municipal League's "Find a Municipality" search
(unlike its "Find an Individual" search, which is explicitly member-
login-gated) is public — each of the ~309 municipalities in it has a
profile with a "Visit Website »" link, no login required.

Two site quirks worth calling out:

1. The search results grid isn't on the main page at all — it's an
   <iframe> pointing straight at a searchserver/people2.aspx URL. This
   navigates directly to that URL rather than fighting the iframe
   indirection, then pages through results via the same ASP.NET
   __doPostBack() calls the "2", "3", ... pager links use. The pager's
   control-ID naming only cleanly covers ctl02 through ctl11 (pages
   2-11) — beyond that the id scheme changes to a "next batch of
   pages" pattern this doesn't follow, so collection plateaus at
   ~257 of the ~309 total. That's a real gap in what this scraper
   reaches, not a dedup artifact.

2. Individual profile pages 403 a plain `requests` GET, and (as with
   Montana) a second top-level navigation in the same browser context
   reliably trips a Cloudflare interstitial — so each profile is
   fetched in its own fresh browser context, same fix as Montana.
"""

import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

SEARCH_PAGE_URL = "https://sdmunicipalleague.org/search/custom.asp?id=7252"
PROFILE_URL = "https://sdmunicipalleague.org/members/?id={id}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_PAGER_CTL = 11  # pager control-ID naming only covers pages 2-11 cleanly


def fetch_municipality_ids(page) -> dict[str, str]:
    for attempt in range(3):
        try:
            page.goto(SEARCH_PAGE_URL, timeout=45000)
            break
        except Exception:
            if attempt == 2:
                raise
            page.wait_for_timeout(2000)
    page.wait_for_timeout(3000)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_timeout(5000)

    people_frame = next(f for f in page.frames if "people2.aspx" in f.url)
    people_page_url = people_frame.url
    people_page = page.context.new_page()
    people_page.goto(people_page_url, timeout=45000)
    people_page.wait_for_timeout(3000)

    def harvest() -> dict[str, str]:
        soup = BeautifulSoup(people_page.content(), "html.parser")
        out = {}
        for a in soup.find_all("a", href=True):
            match = re.search(r"id=(\d+)", a["href"])
            if match and "members" in a["href"]:
                name = a.get_text(strip=True)
                if name and name != "View Profile":
                    out[name] = match.group(1)
        return out

    ids: dict[str, str] = {}
    ids.update(harvest())
    for ctl in range(2, MAX_PAGER_CTL + 1):
        target = f"SearchResultsGrid$ctl28$ctl{ctl:02d}"
        try:
            people_page.evaluate(f"__doPostBack('{target}', '')")
        except Exception:
            break
        people_page.wait_for_timeout(2500)
        ids.update(harvest())

    people_page.close()
    return ids


def fetch_website(browser, municipality_id: str) -> str | None:
    context = browser.new_context(user_agent=USER_AGENT)
    try:
        page = context.new_page()
        for attempt in range(3):
            try:
                page.goto(PROFILE_URL.format(id=municipality_id), timeout=45000)
                break
            except Exception:
                if attempt == 2:
                    raise
                page.wait_for_timeout(2000)
        page.wait_for_timeout(1500)
        soup = BeautifulSoup(page.content(), "html.parser")
        for a in soup.find_all("a", href=True):
            if "visit website" in a.get_text(strip=True).lower():
                return a["href"].strip()
        return None
    finally:
        context.close()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        listing_context = browser.new_context(user_agent=USER_AGENT)
        listing_page = listing_context.new_page()
        municipality_ids = fetch_municipality_ids(listing_page)
        listing_context.close()
        print(f"found {len(municipality_ids)} South Dakota municipality profiles")

        websites: dict[str, str] = {}
        for name, municipality_id in municipality_ids.items():
            website = fetch_website(browser, municipality_id)
            if website:
                websites[name] = website

        browser.close()

    print(f"found a website for {len(websites)} of {len(municipality_ids)} South Dakota municipalities")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in websites.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'South Dakota', 'city', ?, 'sdml-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} South Dakota municipalities indexed")


if __name__ == "__main__":
    main()
