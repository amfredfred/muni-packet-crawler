"""
Scrapes Nebraska.gov's official "Cities / Counties / Villages" attractions
page — a static accordion panel; the "Cities" panel (bounded between its
own id="cities" wrapper and the next "Counties" panel heading) has direct
links to official city websites. Entries with no known site render as
plain text (no <a>) and are skipped naturally.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://www.nebraska.gov/attractions/city-county/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(r'<a href="(https?://[^"]+)" target="_blank">([^<]+)</a>')


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    start = html.find('id="cities" class="panel-collapse')
    end = html.find('id="headingTwo"', start)
    if start == -1 or end == -1:
        print("could not find the Cities panel bounds — page structure may have changed")
        return
    section = html[start:end]

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for website, raw_name in ENTRY_RE.findall(section):
        name = raw_name.strip()
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Nebraska', 'city', ?, 'ne-gov')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Nebraska municipalities indexed")


if __name__ == "__main__":
    main()
