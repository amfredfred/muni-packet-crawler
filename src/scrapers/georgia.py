"""
Two-step scrape of the Georgia Municipal Association member-city directory.
Looked JS-gated at first glance (a Next.js page with a "Load More" button
that reveals 12 more cities per click, no visible network request on
click — ~400+ clicks for all 536 entries). Turned out the click does
nothing but reveal more of an array that's already fully present: the
directory page's initial server-render embeds the ENTIRE 536-city dataset
(name, FIPS code, address, phone, population, district) as an escaped
JSON blob inside a Next.js `self.__next_f.push(...)` RSC chunk. No
clicking, no pagination, no browser automation needed for the list.

The official municipal website isn't in that bulk blob — it's only on
each city's own detail page (`/gma-cities-districts/{slug}/{fips}`,
slug = CompanySort lowercased with spaces turned to hyphens). Each
detail page is a plain 200-status page fetchable with `requests`; the
site's own template marks the official-website link with a
`fa-window-maximize` icon immediately before it, which reliably
distinguishes it from the many other external links (social media,
CDN assets, partner orgs) also present on the page.
"""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection


def write_with_retry(cur, conn, sql, params, attempts=8):
    # crawl_sites.py may be running concurrently (its own long-running
    # writer holds locks past this connection's busy_timeout on occasion),
    # so back off and retry rather than letting one contention blip kill
    # an otherwise-successful 536-entry run.
    for attempt in range(attempts):
        try:
            cur.execute(sql, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return False

LIST_URL = "https://www.gacities.com/gma-cities-districts/gma-member-cities"
DETAIL_URL = "https://www.gacities.com/gma-cities-districts/{slug}/{fips}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# The bulk city array is embedded as an escaped JSON string (\" for each
# quote) inside a Next.js RSC push chunk. Unescaping \" -> " turns it back
# into parseable JSON.
BLOB_START = '[{\\"$type\\"'
WEBSITE_RE = re.compile(r'data-icon="window-maximize"[\s\S]{0,400}?<a href="(https?://[^"]+)"', re.I)


def slugify(company_sort: str) -> str:
    return company_sort.strip().lower().replace(" ", "-")


NAME_PREFIX_RE = re.compile(r"^(city|town) of ", re.I)


def municipality_name(company: str) -> str:
    # `Company` is the actual municipality's proper name (e.g. "City of
    # McDonough", "Athens-Clarke County", "Augusta"). Deliberately NOT using
    # the `City` field — that's the mailing-address town, which can differ
    # from the municipality itself (Arcade, GA mails through Jefferson, GA).
    return NAME_PREFIX_RE.sub("", company.strip())


def fetch_directory(session: requests.Session) -> list[dict]:
    resp = session.get(LIST_URL, timeout=30)
    resp.raise_for_status()
    text = resp.text
    start = text.find(BLOB_START)
    if start == -1:
        raise RuntimeError("member-city data blob not found — page structure may have changed")
    end = text.find("}]", start) + 2
    blob = text[start:end].replace('\\"', '"')
    return json.loads(blob)


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    entries = fetch_directory(session)
    print(f"found {len(entries)} entities in directory")

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for entry in entries:
        company = entry.get("Company", "").strip()
        name = municipality_name(company)
        fips = entry.get("FIPS_CODE", "").strip()
        slug = slugify(entry.get("CompanySort", ""))
        if not name or not fips or not slug:
            print(f"  skipping incomplete entry: {entry}")
            continue
        place_type = "county" if "county" in name.lower() else "city"

        try:
            detail_resp = session.get(DETAIL_URL.format(slug=slug, fips=fips), timeout=30)
        except requests.RequestException as e:
            print(f"  {name}: request failed ({e}), skipping")
            continue
        if detail_resp.status_code != 200:
            print(f"  {name}: HTTP {detail_resp.status_code}, skipping")
            continue

        match = WEBSITE_RE.search(detail_resp.text)
        if not match:
            print(f"  {name}: no official website link found, skipping")
            continue
        website = match.group(1)

        write_with_retry(
            cur,
            conn,
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Georgia', ?, ?, 'gma-directory')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, place_type, website),
        )
        total += 1
        time.sleep(0.2)

    conn.close()
    print(f"done — {total} Georgia municipalities indexed")


if __name__ == "__main__":
    main()
