"""
Maine's own municipal league (memun.org) turned out to be a dead end —
its "Municipal Directory" and "Ordinances & Policies" pages are both
behind a member login wall, confirmed by fetching them directly and
getting a login form back instead of content.

What worked instead: the University of Maine School of Law's "Maine
Local Ordinances" LibGuide (lawguides.mainelaw.maine.edu/ordinances),
one page per county (16 total — Hancock's is hosted at a raw c.php URL
rather than the /ordinances/<county> pattern the other 15 use), each
listing every town in that county with a link to wherever its
ordinances live online. For towns that host ordinances on their own
site (the large majority), that link's domain is the town's real
website; a handful use a third-party code-hosting platform instead
(Municode, eCode360, etc.) — those are dropped rather than stored,
since the platform's domain isn't the town's own site. Rows for the
county government itself and its Registry of Deeds are also dropped by
name pattern, not by domain, since some very small towns' own sites
are hosted as a subpage of the county's domain (e.g. Aroostook's
unorganized-territory towns) and would otherwise be lost too.

Only the domain root is kept (not the specific ordinances sub-page
each link points to) since that's the more useful crawl starting point
for finding meeting packets, which usually live elsewhere on the same
site. Coverage is genuinely partial — towns with no ordinances posted
online at all (the LibGuide says so explicitly for some, e.g. "Contact
town clerk for ordinances") simply aren't in the source.
"""

import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_connection

COUNTIES = [
    "androscoggin", "aroostook", "cumberland", "franklin", "kennebec",
    "knox", "lincoln", "oxford", "penobscot", "piscataquis", "sagadahoc",
    "somerset", "waldo", "washington", "york",
]
# Hancock's guide page predates the clean /ordinances/<county> URL scheme
# the other 15 counties use, and still lives at its original c.php URL.
HANCOCK_URL = "https://lawguides.mainelaw.maine.edu/c.php?g=246970&p=1719019"
HEADERS = {"User-Agent": "Mozilla/5.0"}

THIRD_PARTY_HOSTS = (
    "municode.com", "ecode360.com", "codepublishing.com", "amlegal.com",
    "generalcode.com", "municipalcodeonline.com",
)
SKIP_NAME_RE = re.compile(r"municipalit|registry of deeds|county government|ordinances library", re.IGNORECASE)


def get_with_retry(url: str, tries: int = 4) -> requests.Response:
    last_exc = None
    for attempt in range(tries):
        try:
            return requests.get(url, headers=HEADERS, timeout=25)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc


def extract_towns(soup: BeautifulSoup) -> dict[str, str]:
    content = soup.find("div", id="s-lg-guide-main") or soup
    out: dict[str, str] = {}
    for a in content.find_all("a", href=True):
        href = a["href"].strip()
        name = a.get_text(strip=True).replace("This link opens in a new window", "").strip()
        if not href.startswith("http") or "mainelaw" in href or "libapps" in href:
            continue
        if not name or SKIP_NAME_RE.search(name):
            continue
        netloc = urlparse(href).netloc.lower()
        if any(host in netloc for host in THIRD_PARTY_HOSTS):
            continue
        parsed = urlparse(href)
        out[name] = f"{parsed.scheme}://{parsed.netloc}/"
    return out


def fetch_all_towns() -> dict[str, str]:
    towns: dict[str, str] = {}
    for county in COUNTIES:
        resp = get_with_retry(f"https://lawguides.mainelaw.maine.edu/ordinances/{county}")
        towns.update(extract_towns(BeautifulSoup(resp.text, "html.parser")))
        time.sleep(1)
    resp = get_with_retry(HANCOCK_URL)
    towns.update(extract_towns(BeautifulSoup(resp.text, "html.parser")))
    return towns


def main() -> None:
    towns = fetch_all_towns()
    print(f"fetched {len(towns)} Maine municipalities with an ordinances-source website")

    conn = get_connection()
    cur = conn.cursor()
    total = 0
    for name, website in towns.items():
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, 'Maine', 'town', ?, 'mainelaw-ordinances-libguide')
            ON CONFLICT(state, name) DO UPDATE SET
                official_url=excluded.official_url,
                updated_at=CURRENT_TIMESTAMP
            """,
            (name, website),
        )
        total += 1
    conn.commit()
    conn.close()
    print(f"done — {total} Maine municipalities indexed")


if __name__ == "__main__":
    main()
