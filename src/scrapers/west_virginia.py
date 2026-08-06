"""
Scrapes the West Virginia Municipal League's member-cities page — a static
table of alphabetical columns with links per city. Some smaller
municipalities link to a Wikipedia article instead of an official site
(no official website exists); those are skipped since they're not useful
crawl targets.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://www.wvml.org/about/member-cities"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(r'<td class="tabcol"><a href="(https?://[^"]+)"[^>]*>([^<]+)</a></td>')


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    skipped = 0

    for website, raw_name in ENTRY_RE.findall(resp.text):
        if "wikipedia.org" in website:
            skipped += 1
            continue
        name = raw_name.strip()
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'West Virginia', 'city', ?, 'wv-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} West Virginia municipalities indexed ({skipped} wikipedia-only skipped)")


if __name__ == "__main__":
    main()
