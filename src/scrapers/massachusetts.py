"""
Scrapes Mass.gov's official "Massachusetts City and Town Websites" list —
a state government source (not a league), grouped alphabetically, one
static content block per municipality once rendered. Mass.gov blocks plain
`requests` (bot detection), so this uses headless Chromium via
_playwright_fetch instead.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection
from scrapers._playwright_fetch import fetch_rendered_html

URL = "https://www.mass.gov/lists/massachusetts-city-and-town-websites"

ENTRY_RE = re.compile(
    r'<a href="(https?://[^"]+)" class="js-clickable-link">\s*<span> ([^<]+)</span>'
)


def main() -> None:
    html = fetch_rendered_html(URL)

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for website, raw_name in ENTRY_RE.findall(html):
        name = raw_name.strip()
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Massachusetts', 'city', ?, 'ma-gov')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Massachusetts municipalities indexed")


if __name__ == "__main__":
    main()
