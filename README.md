# muni-packet-crawler

Nationwide pipeline to find upcoming Planning/Zoning (and related) board meeting
agendas and packets directly from municipal government websites — no Google
search step, so no CAPTCHA/ToS risk. Meant to run continuously on a VPS.

Replaces the Google-search-based discovery flow in the sibling
`packet-finder-chrome-extension` project with direct crawling of known
municipal sites.

## Pipeline

1. **`src/build_index.py`** — one-time (re-runnable) index build. Queries
   Wikidata for every US city/town/village with a known official website
   (`P856`) and stores it in `data/muni.db` (`municipalities` table). See the
   module docstring for current place-type coverage and known gaps.
2. **Site crawler** (not yet built) — visits each municipality's official
   site, finds its meetings/agendas page or linked civic platform
   (CivicPlus/Legistar/Granicus/iQM2/eScribe/NovusAgenda/Municode — same host
   list as the Chrome extension), and records upcoming meetings into the
   `meetings` table.
3. **Packet extractor** (not yet built) — for each upcoming meeting, finds the
   packet/agenda PDF link and records it into the `packets` table, so re-runs
   only surface new packets (`reviewed` flag + `discovered_at` timestamp).

## Setup

```bash
pip install -r requirements.txt
python -m playwright install chromium   # only needed for JS-rendered/bot-blocked sources

python src/build_index.py               # Wikidata seed (~100 major cities nationwide)
python src/scrapers/california.py
python src/scrapers/michigan.py
python src/scrapers/colorado.py
python src/scrapers/virginia.py
python src/scrapers/west_virginia.py
python src/scrapers/kansas.py
python src/scrapers/nevada.py
python src/scrapers/new_mexico.py
python src/scrapers/south_carolina.py   # two-step scrape, ~540 requests, several minutes
python src/scrapers/arizona.py          # headless-browser pagination, ~1-2 minutes
python src/scrapers/massachusetts.py    # headless-browser (mass.gov blocks plain requests)
```

`data/muni.db` is a plain SQLite file — inspect it with any SQLite browser or
`sqlite3 data/muni.db`.

## State coverage

Current total: **2,585 municipalities across 30 states.**

| State | Source | Count | Notes |
|---|---|---|---|
| Michigan | `scrapers/michigan.py` | 485 | 26 letter-pages, "Full Member" filter |
| California | `scrapers/california.py` | 466 | static HTML |
| Massachusetts | `scrapers/massachusetts.py` | 349 | official mass.gov list (state gov, not a league); headless browser (site blocks plain requests) |
| Kansas | `scrapers/kansas.py` | 298 | static HTML |
| Colorado | `scrapers/colorado.py` | 215 | static HTML |
| Virginia | `scrapers/virginia.py` | 202 | cities+towns+counties, archived static page |
| South Carolina | `scrapers/south_carolina.py` | 198 | two-step (list page → per-city filtered page → profile page); ~73 members have no listed site |
| West Virginia | `scrapers/west_virginia.py` | 92 | ~137 members have no site (Wikipedia link only, skipped) |
| Arizona | `scrapers/arizona.py` | 83 | WP Business Directory plugin, paginated client-side; headless browser clicks through all pages |
| New Mexico | `scrapers/new_mexico.py` | 80 | static HTML |
| Nevada | `scrapers/nevada.py` | 19 | small state; includes General Improvement Districts |
| (remaining ~19 states) | `build_index.py` (Wikidata) | ~1-23 each | major cities only, sparse — P856 rarely set for small municipalities |

### Checked and ruled out (with reason)

- **Hard bot-block, even with headless Chromium** (Cloudflare/nginx-level, would need stealth plugin or residential proxy — out of scope here): Florida, Indiana, Wisconsin.
- **Site outage at check time, not a block** (worth retrying later): Missouri.
- **JS-rendered with no discoverable data source** (tried headless browser, content never materialized or pagination yielded near-zero return per click): Ohio (CivicPlus QuickLinks widget, likely needs a session-gated AJAX call), Georgia (Next.js "Load More" — ~1 new city per click, would need 400+ clicks for full 536; not pursued further).
- **Directory exists but doesn't expose an official government website field** (only meeting schedule/contact info): Mississippi, Wyoming.
- **Dead/unreachable link** at check time: Montana (legacy.mtleague.org does not resolve).
- **Messy/inconsistent markup, not worth the parser effort for a small state**: Delaware (nested accordion structure, most heading levels didn't carry a consistent municipality name).
- **No free public directory found at all** (paid/printed directory only): Illinois, New Jersey, Pennsylvania, Minnesota, Iowa, Kentucky, Tennessee, Oregon (PDF only), Texas, North Carolina, Alabama, Louisiana, Maryland, Oklahoma, Idaho, North Dakota, South Dakota, Utah, Connecticut, Maine, New Hampshire, Vermont, Rhode Island.
- **No sub-county municipal government exists** (nothing to index): Hawaii (only 5 counties; Honolulu is consolidated city-county).
- **Two-step, not yet built**: Alaska (per-municipality PDF profile sheets on akml.org — would need PDF parsing, ~165 municipalities).

### Adding a new state

Follow the pattern in `src/scrapers/`: fetch the state municipal league's
member/city directory page with a browser-like `User-Agent` (fall back to
`_playwright_fetch.py`'s headless Chromium if the site blocks plain
`requests` or renders the list client-side), write a scoped regex (bind to
the specific wrapping tag/class, not a bare `<a href>` — a loose pattern
will pick up nav/footer links as fake municipalities), and upsert into
`municipalities` keyed on `(state, name)`.
