"""
The Association of Idaho Cities' member directory search (/page/directory)
is member-login-gated, but its public "Cities in Idaho" page
(idahocities.org/?page=Cities) isn't — it's a plain static list of every
Idaho city with a link to its official website, no login or search
required. The site sits behind bot-detection that 403s a plain
`requests` call (confirmed on every path tried, including the bare
domain), so this goes through headless Chromium via the shared
_playwright_fetch helper instead, same as the Arizona/Massachusetts
scrapers.

The list has some HTML quirks worth handling rather than working around
with special cases: most city names are prefixed with "*" (an AIC
member-status marker, not part of the name), a few cities appear twice
(once as a bare "*" link wrapping an icon, once as the actual text
link) pointing at the same URL, and one entry has a doubled scheme
("http://https://..."). Dedup by cleaned city name keeps the first
good URL seen and a light regex fixes the doubled-scheme typo rather
than dropping that city.
"""

import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection
from scrapers._playwright_fetch import fetch_rendered_html

LIST_URL = "https://idahocities.org/?page=Cities"

NON_CITY_HOST_FRAGMENTS = (
    "idahocities.org",
    "yourmembership.com",
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "youtube.com",
    "addtoany.com",
)
DOUBLED_SCHEME_RE = re.compile(r"^https?://(https?://)", re.IGNORECASE)


def fetch_cities() -> dict[str, str]:
    html = fetch_rendered_html(LIST_URL, wait_ms=6000, timeout_ms=45000)
    soup = BeautifulSoup(html, "html.parser")

    out: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http") or any(frag in href for frag in NON_CITY_HOST_FRAGMENTS):
            continue
        name = a.get_text(strip=True).lstrip("*").strip()
        if not name:
            continue
        href = DOUBLED_SCHEME_RE.sub(r"\1", href)
        if name not in out:
            out[name] = href
    return out


def main() -> None:
    cities = fetch_cities()
    print(f"fetched {len(cities)} Idaho cities")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in cities.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Idaho', 'city', ?, 'aic-cities-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Idaho cities indexed")


if __name__ == "__main__":
    main()
