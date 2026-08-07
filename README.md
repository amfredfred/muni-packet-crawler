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
python src/scrapers/nebraska.py
python src/scrapers/alabama.py          # two-step, ~460 profile-page requests, several minutes
python src/scrapers/rhode_island.py     # two-step via entity IDs already in the static HTML
python src/scrapers/georgia.py          # two-step, ~536 detail-page requests, several minutes
python src/scrapers/minnesota.py        # static snapshot — see state coverage notes below
```

`data/muni.db` is a plain SQLite file — inspect it with any SQLite browser or
`sqlite3 data/muni.db`.

## State coverage

Current total: **3,467 municipalities across 33 states.**

| State | Source | Count | Notes |
|---|---|---|---|
| Michigan | `scrapers/michigan.py` | 485 | 26 letter-pages, "Full Member" filter |
| California | `scrapers/california.py` | 466 | static HTML |
| Georgia | `scrapers/georgia.py` | 412 | two-step: the visible "Load More" button does nothing but reveal more of an array that's already fully present — the whole 536-city dataset (name, FIPS code, address, phone) is embedded server-side in the page's initial Next.js RSC payload. Official website is only on each city's own detail page (plain `requests`, no clicking); 125 of 536 have no website listed at all, a genuine gap in GMA's own data |
| Massachusetts | `scrapers/massachusetts.py` | 352 | official mass.gov list (state gov, not a league); headless browser (site blocks plain requests) |
| Kansas | `scrapers/kansas.py` | 298 | static HTML |
| Alabama | `scrapers/alabama.py` | 235 | two-step: Atlas Alabama's directory links to per-municipality profile pages, each with a labeled "City Website"/"Town Website" button (Alabama classifies municipalities as either — matching only one form silently skipped every instance of the other on the first pass) |
| Colorado | `scrapers/colorado.py` | 215 | static HTML |
| Virginia | `scrapers/virginia.py` | 204 | cities+towns+counties, archived static page |
| South Carolina | `scrapers/south_carolina.py` | 198 | two-step (list page → per-city filtered page → profile page); ~73 members have no listed site |
| Nebraska | `scrapers/nebraska.py` | 144 | official nebraska.gov attractions page, static accordion panel |
| West Virginia | `scrapers/west_virginia.py` | 92 | ~137 members have no site (Wikipedia link only, skipped) |
| Arizona | `scrapers/arizona.py` | 83 | WP Business Directory plugin, paginated client-side; headless browser clicks through all pages |
| New Mexico | `scrapers/new_mexico.py` | 83 | static HTML |
| Minnesota | `scrapers/minnesota.py` | 57 | mn.gov's directory sits behind Radware Bot Manager, but that block only triggers on a *headless* fingerprint — a real (non-headless) browser session passes through with no CAPTCHA at all. Of ~850 listed cities, only 57 have an actual website link (the rest are unlinked plain text, a real gap in the state's own data); captured as a static snapshot since re-running requires a real browser session, not a re-runnable headless script |
| Rhode Island | `scrapers/rhode_island.py` | 40 | two-step: each row's `onclick` handler resolves to a plain `GET .../GOVDirectoryMeetingDetails?EntityID=N` with no JS execution needed; includes the Narragansett Indian Tribe alongside cities/towns |
| Nevada | `scrapers/nevada.py` | 19 | small state; includes General Improvement Districts |
| (remaining ~19 states) | `build_index.py` (Wikidata) | ~1-23 each | major cities only, sparse — P856 rarely set for small municipalities |

### Checked and ruled out (with reason)

- **Hard bot-block, even with headless Chromium** (Cloudflare/nginx-level, would need stealth plugin or residential proxy — out of scope here): Florida, Indiana, Wisconsin, Kentucky.
- **Site outage at check time, not a block** (worth retrying later): Missouri.
- **Content genuinely no longer exists at the flagged URL**: Ohio's `omlohio.org` QuickLinks category that used to list cities/villages has been removed/reorganized (now returns "There are no Quick Links for this selection"). The closest replacement found via the site's own search is a members-only PDF roster — names only, no website URLs or link annotations, so it can't populate `official_url`.
- **Directory exists but doesn't expose an official government website field** (only meeting schedule/contact info): Mississippi, Wyoming.
- **Dead/unreachable domain** at check time (confirmed with both plain `requests` and headless Chromium — genuine connectivity issue, not a bot-block): Montana (`legacy.mtleague.org`), North Dakota (`nd.gov` — notable since this was otherwise the best-named lead, a page literally titled "City Website List"), Delaware (`dllg.delaware.gov`, a second attempt after the original `ipa.udel.edu` source proved too messy to parse — also fails DNS resolution).
- **Membership-gated** (directory requires login): Idaho.
- **No free public directory found at all** (paid/printed directory only, or only a general state-facts page with no city-level list): Illinois, New Jersey, Pennsylvania, Iowa, Tennessee, Oregon (PDF only), Texas, North Carolina, Louisiana, Maryland, Oklahoma, South Dakota, Utah, Connecticut, Maine (state portal is a per-town search tool, not a flat list), New Hampshire, Vermont, Arkansas.
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
