"""
The Montana League of Cities and Towns runs its whole site behind bot
detection that 403s a plain `requests` GET (confirmed on both the
directory listing and an individual profile page), so this goes
through headless Chromium throughout.

The listing page (/about/municipal-directory) renders all ~127 member
cards server-side in one load, each linking to a per-municipality
profile page (/directory/<slug>). Each profile publicly shows a
"Website:" field with no login required (only the extended contact
list — officials' names/emails — is member-gated), so no per-city
inference or verification step is needed here, unlike Kentucky/
Mississippi: the League already states the website directly. The
listed href sometimes omits the scheme entirely (just "albertonmontana
.com"), so that's normalized to https:// before storing.

One more wrinkle: a second navigation within the same browser context
reliably tripped a Cloudflare "Just a moment..." interstitial, even
with a several-second pause between requests — not a request-rate
issue, since spacing it out further didn't help. A brand new browser
context per profile page (still the same browser process, so this
isn't expensive) sidesteps whatever per-context heuristic triggers it,
confirmed by testing fresh contexts back-to-back successfully.
"""

import sys
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

LIST_URL = "https://mtleague.org/about/municipal-directory"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_profile_links(page) -> dict[str, str]:
    page.goto(LIST_URL, timeout=45000, wait_until="load")
    page.wait_for_timeout(3000)
    soup = BeautifulSoup(page.content(), "html.parser")

    out: dict[str, str] = {}
    for card in soup.select("div.card"):
        heading = card.find("h2")
        link = card.find("a", href=True)
        if not heading or not link:
            continue
        href = link["href"]
        if "/directory/" not in href:
            continue
        if not href.startswith("http"):
            href = f"https://mtleague.org{href}"
        out[heading.get_text(strip=True)] = href
    return out


def fetch_website(browser, profile_url: str) -> str | None:
    context = browser.new_context(user_agent=USER_AGENT)
    try:
        page = context.new_page()
        for attempt in range(3):
            try:
                page.goto(profile_url, timeout=45000, wait_until="load")
                break
            except Exception:
                if attempt == 2:
                    raise
                page.wait_for_timeout(2000)
        page.wait_for_timeout(1500)
        soup = BeautifulSoup(page.content(), "html.parser")

        for heading in soup.find_all("h6"):
            if "website" not in heading.get_text(strip=True).lower():
                continue
            link = heading.find_next("a", href=True)
            if not link:
                return None
            href = link["href"].strip()
            if not href.startswith("http"):
                href = f"https://{href}"
            return href
        return None
    finally:
        context.close()


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        listing_page = browser.new_page(user_agent=USER_AGENT)
        profiles = fetch_profile_links(listing_page)
        listing_page.close()
        print(f"found {len(profiles)} Montana municipality profiles")

        websites: dict[str, str] = {}
        for name, profile_url in profiles.items():
            website = fetch_website(browser, profile_url)
            if website:
                websites[name] = website

        browser.close()

    print(f"found a website for {len(websites)} of {len(profiles)} Montana municipalities")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in websites.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Montana', 'city', ?, 'mtleague-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Montana municipalities indexed")


if __name__ == "__main__":
    main()
