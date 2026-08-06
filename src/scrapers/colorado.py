"""
Scrapes the Colorado Municipal League's static member directory page — a
single alphabetical <li> list with direct links to official websites.
Entries with no known website render as plain text (no <a>) and are skipped
naturally since the regex requires an href.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://www.cml.org/utility-pages/cml-member-directory"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(r'<li><a[^>]+href="(https?://[^"]+)"[^>]*>([^<]+)</a></li>')


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for website, raw_name in ENTRY_RE.findall(resp.text):
        name = raw_name.replace("*", "").strip()
        if not name:
            continue
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Colorado', 'city', ?, 'co-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Colorado municipalities indexed")


if __name__ == "__main__":
    main()
