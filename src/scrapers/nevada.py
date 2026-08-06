"""
Scrapes the Nevada League of Cities & Municipalities member-municipalities
page. Built with Elementor (WordPress page builder): each member's heading
and its official-site logo-link appear in the same order but not co-located
in the markup, so this zips two separately-extracted lists by position
(verified 1:1, 19 headings / 19 links) rather than a single combined regex.
General Improvement Districts (GIDs) are included with place_type='district'.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

URL = "https://nvleague.com/member-municipalities/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

NAME_RE = re.compile(r'<h4 class="title second_font">\s*([^<]+?)\s*</h4>')
LINK_RE = re.compile(r'<a href="(https?://(?!nvleague)[^"]+)" target="_blank">\s*<img')


def classify(name: str) -> str:
    if "GID" in name:
        return "district"
    return "city"


def clean_name(name: str) -> str:
    for prefix in ("City of ", "Town of "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def main() -> None:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    names = NAME_RE.findall(resp.text)
    links = LINK_RE.findall(resp.text)
    if len(names) != len(links):
        print(f"WARNING: {len(names)} names vs {len(links)} links — mismatch, aborting")
        return

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for raw_name, website in zip(names, links):
        name = clean_name(raw_name)
        place_type = classify(raw_name)
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Nevada', ?, ?, 'nv-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                place_type=excluded.place_type,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, place_type, website),
        )
        total += 1

    conn.commit()
    conn.close()
    print(f"done — {total} Nevada municipalities/districts indexed")


if __name__ == "__main__":
    main()
