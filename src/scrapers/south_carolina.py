"""
Two-step scrape of the Municipal Association of South Carolina's directory
(Drupal-based): the main directory page exposes a `<select name="municipality">`
with every member city/town and a numeric id, but no website link. Filtering
by id (`?municipality=<id>`) reveals the city's profile-page slug
(`/municipality/<slug>`), and that profile page contains the actual official
government website URL in its body text. So: list page (already static, no
extra request) -> filtered page per city (get slug) -> profile page (get
website). ~2 requests per city; rate-limited to be polite.
"""

import html
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

BASE_URL = "https://www.masc.sc/publications/municipal-online-directory"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

MUNICIPALITY_SELECT_RE = re.compile(
    r'<select data-drupal-selector="edit-municipality".*?</select>', re.S
)
OPTION_RE = re.compile(r'<option value="(\d+)"[^>]*>([^<]+)</option>')
SLUG_RE_TEMPLATE = r'/municipality/([a-z0-9-]+)"[^>]*>{name}<'
WEBSITE_RE = re.compile(
    r'field--name-website-url.*?<a href="(https?://[^"]+)"', re.S
)


def get_municipality_options(session: requests.Session) -> list[tuple[str, str]]:
    resp = session.get(BASE_URL, timeout=30)
    resp.raise_for_status()
    select_match = MUNICIPALITY_SELECT_RE.search(resp.text)
    if not select_match:
        return []
    options = OPTION_RE.findall(select_match.group(0))
    return [(mid, html.unescape(name)) for mid, name in options if mid != "All"]


def get_slug(session: requests.Session, mid: str, name: str) -> str | None:
    resp = session.get(BASE_URL, params={"municipality": mid}, timeout=30)
    resp.raise_for_status()
    pattern = re.compile(SLUG_RE_TEMPLATE.format(name=re.escape(name)))
    m = pattern.search(resp.text)
    return m.group(1) if m else None


def get_website(session: requests.Session, slug: str) -> str | None:
    resp = session.get(f"https://www.masc.sc/municipality/{slug}", timeout=30)
    if resp.status_code != 200:
        return None
    m = WEBSITE_RE.search(resp.text)
    return m.group(1) if m else None


def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    options = get_municipality_options(session)
    print(f"found {len(options)} municipalities in directory")

    conn = get_connection()
    cur = conn.cursor()
    total = 0

    for mid, name in options:
        name = name.strip()
        try:
            slug = get_slug(session, mid, name)
            if not slug:
                print(f"  {name}: could not find profile slug, skipping")
                continue
            time.sleep(0.3)
            website = get_website(session, slug)
            if not website:
                print(f"  {name}: no website found on profile page, skipping")
                continue
        except requests.RequestException as e:
            print(f"  {name}: request failed ({e}), skipping")
            continue

        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'South Carolina', 'city', ?, 'sc-league')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
        conn.commit()
        time.sleep(0.3)

    conn.close()
    print(f"done — {total} South Carolina municipalities indexed")


if __name__ == "__main__":
    main()
