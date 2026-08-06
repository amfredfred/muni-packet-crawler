"""
Scrapes the Kansas Association of City/County Management's "City Websites"
page — a single static alphabetical <ul><li> list maintained in partnership
with the League of Kansas Municipalities. Entries with no known site render
as plain text (no <a>) and are skipped naturally.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://kacm.us/page/city-websites"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

ENTRY_RE = re.compile(r'<li><a href="(https?://[^"]+)"><span[^>]*>([^<]+)</span></a></li>')


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for website, raw_name in ENTRY_RE.findall(resp.text):
        name = raw_name.strip()
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Kansas', 'city', ?, 'ks-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Kansas municipalities indexed")


if __name__ == "__main__":
    main()
