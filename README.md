# muni-packet-crawler

Nationwide pipeline to find upcoming Planning/Zoning (and related) board meeting
agendas and packets directly from municipal government websites — no Google
search step, so no CAPTCHA/ToS risk. Includes a local control-panel dashboard
(2FA-gated) to run/monitor the crawl, and a separate business-invoice generator
bolted onto the same webapp for unrelated billing needs.

Replaces the Google-search-based discovery flow in the sibling
`packet-finder-chrome-extension` project with direct crawling of known
municipal sites.

## Pipeline

1. **`src/build_index.py`** + **`src/scrapers/*.py`** — one-time (re-runnable)
   index build. Seeds `municipalities` from Wikidata (major cities nationwide)
   plus 16 state-specific scrapers for state municipal-league directories that
   have much better coverage than Wikidata for small towns. See "State
   coverage" below for the current total and per-state notes.
2. **`src/crawl_sites.py`** — visits each municipality's official site, ranks
   candidate meetings/agendas pages (a link naming the exact board type
   scores highest, then a known civic-platform host, then generic
   agenda/meeting keywords), and extracts packet/agenda links into `meetings`
   and `packets`. Three fallback mechanisms beyond a single-page scan:
   - **Sub-page following**: a page that looks like an individual meeting
     detail row (matches a date or "meeting"/"agenda" text) gets visited too,
     up to `FOLLOW_LIMIT`.
   - **Nested nav-menu fallback**: if the homepage names nothing directly
     (no board-matching link, no civic-platform host, no agenda keyword), it
     tries generic "Government"/"Departments"/"Boards & Commissions" nav
     links one level deep before giving up — some sites bury the real link
     behind a multi-level government dropdown menu rather than linking it
     from the homepage at all.
   - **PDF-embedded-link following**: once a packet PDF is confirmed, that
     PDF's own content is scanned for links to *other* PDFs — both real
     link annotations and plain-text URLs printed in the body. Some
     municipalities host the actual packet at a separate URL that's only
     ever referenced from inside the agenda PDF, never from any HTML page.
   Concurrent (thread pool, `--workers`, default 20) — see the module
   docstring for the single-writer-thread architecture that keeps SQLite
   from locking up under concurrent crawls.
3. **`src/review_packets.py`** + **`src/pdf_review.py`** — best-effort review
   of each packet's PDF: native text extraction (pypdf) first, OCR
   (Tesseract + Poppler, see Setup) as a fallback when the PDF has no text
   layer at all (i.e. it's a scanned image). Flags a packet as `priority`
   when its text names a specific applicant/owner *and* a contact email —
   for license/permit-style board types specifically, this same check gates
   whether the packet is even saved in the first place (see
   `passes_license_content_gate`), since routine agendas never contain this
   combination but genuine applications do.
4. **`src/export_seed_db.py`** — exports the static, safe-to-ship tables
   (`municipalities`, `meetings`, `packets`, `blocked_sources`) into a
   separately-committed `data/muni_seed.db`. `data/muni.db` itself is never
   committed — it also holds the live 2FA secret and, once used, real
   business/invoice data. `db.get_connection()` auto-seeds a fresh `muni.db`
   from `muni_seed.db` the first time anything runs on a machine that
   doesn't have one yet (e.g. a freshly cloned VPS), so the multi-year
   scraping effort doesn't have to be repeated per deployment.
5. **`src/webapp.py`** — the dashboard: start/stop the crawl and review
   engines with live log tails, browse municipalities/packets with filters
   (state, board type, date range, priority, reviewed status), retry
   blocked state-directory sources, and the invoice generator (below).

## Setup

```bash
python -m venv venv
venv\Scripts\pip install -e ".[ocr]"    # [ocr] optional — see OCR below
venv\Scripts\python -m playwright install chromium   # only for JS-rendered/bot-blocked scrapers

venv\Scripts\python src/build_index.py               # Wikidata seed (~100 major cities nationwide)
venv\Scripts\python src/scrapers/<state>.py           # repeat per state — see State coverage below
```

`data/muni.db` is a plain SQLite file — inspect it with any SQLite browser or
`sqlite3 data/muni.db`. On a fresh clone it doesn't exist yet; it's created
and auto-seeded from the committed `data/muni_seed.db` the first time
anything connects (webapp, crawl, review, or any scraper).

### OCR (optional)

Native PDF text extraction (pypdf) handles the large majority of packets —
OCR is only a fallback for scanned-image PDFs with no text layer at all.
Requires two system binaries this project does not install for you:

- **Tesseract**: https://github.com/UB-Mannheim/tesseract/wiki — the Windows
  installer supports a silent, no-admin-rights install to a per-user
  directory (`tesseract-ocr-w64-setup-*.exe /S /D=C:\Users\<you>\AppData\Local\Programs\Tesseract-OCR`),
  though it may install to `C:\Program Files\Tesseract-OCR` instead depending
  on whether UAC grants elevation. `pdf_review.py` checks both locations (and
  PATH) automatically — no environment-variable changes needed.
- **Poppler** (for `pdf2image`'s PDF→image rendering): a portable Windows
  build, e.g. https://github.com/oschwartz10612/poppler-windows/releases —
  unzip anywhere; `pdf_review.py` also checks
  `%LOCALAPPDATA%\Programs\poppler\poppler-*\Library\bin\` automatically
  (globbed, not version-pinned, so a later upgrade doesn't need a code
  change).

If neither is found, OCR is skipped and `review_method` reports
`ocr_unavailable` rather than failing — this whole module is a best-effort
pass regardless (OCR text can carry misrecognized punctuation/spacing, so
findings are leads to review, not confirmed facts).

## Running the dashboard

```bash
venv\Scripts\python src/webapp.py    # or: venv\Scripts\muni-webapp
```

Open `http://localhost:5000`. First visit shows a 2FA setup page (scan the
QR code with any TOTP authenticator app); every visit after that requires a
6-digit code. Only one registration is allowed at a time — to allow a new
one, delete the row directly (`DELETE FROM auth_totp` in `data/muni.db`),
which the dashboard has no button for on purpose (this is a security reset,
not a routine action).

From the dashboard you can start/stop the crawl and review engines (with
live log tails), browse/filter municipalities and packets, retry blocked
state-directory sources, and manage the invoice generator.

### Running on Windows via Task Scheduler

`install.ps1` registers the webapp (persistent, starts at logon) and the
crawl/review engines (daily batch jobs at 2 AM) as Windows Scheduled Tasks,
so they survive reboots without a terminal window staying open:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1                    # install everything
powershell -ExecutionPolicy Bypass -File install.ps1 -Component webapp update   # re-register one component after a code change
powershell -ExecutionPolicy Bypass -File install.ps1 uninstall           # remove everything
```

Each component launches via `pythonw.exe` (the windowless Python
interpreter) rather than the console-mode `muni-*.exe` wrappers pip
generates, so nothing pops up a terminal at logon or at the nightly batch
times. Under `pythonw.exe`, `sys.stdout`/`sys.stderr` start out as `None` —
each entry point calls `db.redirect_console_if_headless()` as close to the
top of the file as possible (before any third-party imports that could
themselves fail) so a crash still lands in `data/webapp.log` /
`data/crawl_engine.log` / `data/review_engine.log` instead of vanishing
with zero trace. Re-running the installer also cleans up orphaned processes
from manually-started instances (matched by entry-point script name, not
just the full project path, so a `cd project && python src\webapp.py`-style
manual run gets caught too) — Werkzeug's dev server sets `SO_REUSEADDR`, so
two instances bound to the same port don't even error, they just silently
coexist with unpredictable routing.

### Deploying to a Linux VPS

Not yet done for this project (no confirmed terminal/SSH access to a target
server as of this writing) — see `deploy/README.md` for a prepared
systemd-based setup script, ready to run once access is available.

## Invoice generator

A general-purpose business invoice tool sharing the same webapp, unrelated
to the crawler itself and with no database table of its own — an invoice
exists only as the PDF it renders to, nothing is ever saved server-side.

- **Invoices** (`/invoices`) — two tabs: "Create Invoice" (a single JSON
  textarea is the entire editing surface, plus a live preview pane showing
  the actual generated PDF, refreshing as you type) and "How to use it"
  (a field reference and a full copy-pasteable example JSON — load it,
  tweak the values, download). `invoice_number` is part of the JSON and
  freely editable; nothing checks it for uniqueness since there's nothing
  to check it against. The logo is uploaded fresh per invoice (no saved
  default to fall back to). Submitting downloads the PDF directly
  (`Content-Disposition: attachment`) — there's no list of past invoices
  and no edit-an-existing-invoice flow; re-generate from the same JSON
  (kept wherever you want — your own notes, a saved file) whenever it's
  needed again. PDF rendering via reportlab: fitted header logo, faint
  background watermark, configurable accent color.

## State coverage

Current total: **5,878 municipalities across 47 states.**

| State | Source | Count | Notes |
|---|---|---|---|
| Iowa | `scrapers/iowa.py` | 537 | League's "Cities in Iowa" page renders its full member table server-side in one load; ~43% of rows have no website on file (a real gap), a few more have a doubled scheme (`http://https://...`) fixed rather than dropped |
| Michigan | `scrapers/michigan.py` | 485 | 26 letter-pages, "Full Member" filter |
| California | `scrapers/california.py` | 466 | static HTML |
| North Carolina | `scrapers/north_carolina.py` | 457 | UNC School of Government's public "N.C. Cities and Towns" reference table (city/chamber/county columns — only the first is kept) |
| Georgia | `scrapers/georgia.py` | 412 | two-step: the visible "Load More" button does nothing but reveal more of an array that's already fully present — the whole 536-city dataset (name, FIPS code, address, phone) is embedded server-side in the page's initial Next.js RSC payload. Official website is only on each city's own detail page (plain `requests`, no clicking); 125 of 536 have no website listed at all, a genuine gap in GMA's own data |
| Massachusetts | `scrapers/massachusetts.py` | 352 | official mass.gov list (state gov, not a league); headless browser (site blocks plain requests) |
| Kansas | `scrapers/kansas.py` | 298 | static HTML |
| Maine | `scrapers/maine.py` | 294 | Maine's own league gates its directory behind login; a University of Maine School of Law LibGuide (16 county pages, one town per ordinances link) covers 294 of ~486 towns instead — third-party ordinance hosts (Municode etc.) are filtered out since that's not the town's own site |
| Alabama | `scrapers/alabama.py` | 235 | two-step: Atlas Alabama's directory links to per-municipality profile pages, each with a labeled "City Website"/"Town Website" button (Alabama classifies municipalities as either — matching only one form silently skipped every instance of the other on the first pass) |
| Colorado | `scrapers/colorado.py` | 215 | static HTML |
| Virginia | `scrapers/virginia.py` | 204 | cities+towns+counties, archived static page |
| Arkansas | `scrapers/arkansas.py` | 199 | wpDataTables AJAX endpoint on the member directory needed a full 17-column DataTables payload replayed (captured via Playwright), not just draw/start/length; 6,396 official-level rows collapse to 500 cities, 199 with a website on file |
| South Carolina | `scrapers/south_carolina.py` | 198 | two-step (list page → per-city filtered page → profile page); ~73 members have no listed site |
| Idaho | `scrapers/idaho.py` | 163 | AIC's member-search directory is login-gated, but its public "Cities in Idaho" page isn't — 163 of ~200 cities have a link there |
| Maryland | `scrapers/maryland.py` | 151 | MML's "Member Cities and Towns" page is a clean static list, no login/AJAX needed |
| Nebraska | `scrapers/nebraska.py` | 144 | official nebraska.gov attractions page, static accordion panel |
| Kentucky | `scrapers/kentucky.py` | 139 | KLC has no public city directory at all; the state DLG's per-city profile system (`City_ID` 1-495) states one contact email per city, and that email's domain is usually the city's own — verified live and content-checked (see `_domain_verify.py`) rather than trusted on domain name alone, after a first pass let ISPs/phone co-ops through |
| Vermont | `scrapers/vermont.py` | 132 | VLCT's directory is login-gated and the Secretary of State's main site is WAF-blocked, but the SOS's town clerk guide PDF (a different subdomain) isn't; same email-domain-inference-and-verify approach as Kentucky |
| South Dakota | `scrapers/south_dakota.py` | 124 | League's "Find a Municipality" search (unlike "Find an Individual") isn't login-gated; results grid is an iframe hitting an ASP.NET search endpoint directly, paginated via `__doPostBack`, each profile has a plain "Visit Website" link |
| West Virginia | `scrapers/west_virginia.py` | 92 | ~137 members have no site (Wikipedia link only, skipped) |
| Arizona | `scrapers/arizona.py` | 83 | WP Business Directory plugin, paginated client-side; headless browser clicks through all pages |
| New Mexico | `scrapers/new_mexico.py` | 83 | static HTML |
| Montana | `scrapers/montana.py` | 81 | League directory's per-municipality profile states a "Website:" field directly, no login needed; a second top-level navigation in the same browser context reliably tripped a Cloudflare interstitial, fixed with a fresh context per profile |
| Minnesota | `scrapers/minnesota.py` | 57 | mn.gov's directory sits behind Radware Bot Manager, but that block only triggers on a *headless* fingerprint — a real (non-headless) browser session passes through with no CAPTCHA at all. Of ~850 listed cities, only 57 have an actual website link (the rest are unlinked plain text, a real gap in the state's own data); captured as a static snapshot since re-running requires a real browser session, not a re-runnable headless script |
| Delaware | `scrapers/delaware.py` | 51 | the Delaware League of Local Governments' membership-directory page (a different domain from the two earlier dead ends tried) is a clean static list; 3 counties/levy-courts filtered out by name |
| Wyoming | `scrapers/wyoming.py` | 41 | WAM's directory page itself has no website field, but its downloadable "2026 Municipal Directory" PDF states one directly for 41 of 99 towns |
| Rhode Island | `scrapers/rhode_island.py` | 40 | two-step: each row's `onclick` handler resolves to a plain `GET .../GOVDirectoryMeetingDetails?EntityID=N` with no JS execution needed; includes the Narragansett Indian Tribe alongside cities/towns |
| Mississippi | `scrapers/mississippi.py` | 38 | MML's directory (list + per-city profile) has no website or email field at all; the Secretary of State's Blue Book PDF lists a contact email per city instead, same domain-inference-and-verify approach as Kentucky |
| Hawaii | `scrapers/hawaii.py` | 4 | Hawaii has no incorporated cities/towns at all — local government is done entirely by the four county governments (Honolulu is consolidated city-county); those are indexed as `place_type='county'`, matching how Virginia/Georgia already store county-level and consolidated entries |
| Nevada | `scrapers/nevada.py` | 19 | small state; includes General Improvement Districts |
| (remaining states) | `build_index.py` (Wikidata) | ~1-23 each | major cities only, sparse — P856 rarely set for small municipalities |

### Checked and ruled out (with reason)

- **Hard bot-block, even with headless Chromium** (Cloudflare/nginx-level, would need stealth plugin or residential proxy — out of scope here): Florida, Indiana, Wisconsin.
- **Site outage at check time, not a block** (worth retrying later): Missouri.
- **Content genuinely no longer exists at the flagged URL**: Ohio's `omlohio.org` QuickLinks category that used to list cities/villages has been removed/reorganized (now returns "There are no Quick Links for this selection"). The closest replacement found via the site's own search is a members-only PDF roster — names only, no website URLs or link annotations, so it can't populate `official_url`.
- **Directory exists but doesn't expose an official government website field anywhere** (checked the member list, per-city profile pages, and raw HTML for hidden links): Louisiana.
- **League directory is CAPTCHA-walled** (won't attempt to bypass), and the one alternate source with clean structured data (a third-party city-directory site with schema.org markup) is impractically slow — 30-100+ seconds per request across ~350 cities: North Dakota.
- **Directory is a person/official search tool, not a city index** (no per-city website field anywhere in it); the state's own official municipality contact list has names only, no email or website: Oklahoma.
- **No free public directory found at all** (paid/printed directory only, or only a general state-facts page with no city-level list): Illinois, New Jersey, Pennsylvania, Tennessee, Oregon (PDF only), Texas, Utah, Connecticut, New Hampshire.
- **Two-step, not yet built**: Alaska (per-municipality PDF profile sheets on akml.org — would need PDF parsing, ~165 municipalities).

### Adding a new state

Follow the pattern in `src/scrapers/`: fetch the state municipal league's
member/city directory page with a browser-like `User-Agent` (fall back to
`_playwright_fetch.py`'s headless Chromium if the site blocks plain
`requests` or renders the list client-side), write a scoped regex (bind to
the specific wrapping tag/class, not a bare `<a href>` — a loose pattern
will pick up nav/footer links as fake municipalities), and upsert into
`municipalities` keyed on `(state, name)`.

If the league directory has no website field at all, check whether it (or
a state agency's directory/PDF) at least lists a contact email per
municipality — the email's domain is often the city's own. Don't trust
that inference on domain-liveness alone: reuse `scrapers/_domain_verify.py`,
which also checks that the page's title actually names the city and vetoes
obvious non-government domains (ISPs, phone co-ops, utilities), after an
earlier version let a few of those through.

## Security notes

- `data/muni.db`, `data/.flask_secret_key`, `data/*.log`, and any uploaded
  business logo are all gitignored — never commit them. `muni.db` holds the
  live 2FA secret and real business/invoice data once those features are
  used; committing it once already happened by accident (a corrupted
  `.gitignore` pattern silently stopped matching it) and required rotating
  both secrets. `data/muni_seed.db` is the intentionally-committed subset
  (static crawl data only) — see `src/export_seed_db.py`.
- The Flask session-signing key is generated once and persisted to
  `data/.flask_secret_key` (not regenerated per run, so sessions survive a
  restart) — but must never be committed, since anyone with it can forge an
  authenticated session cookie without knowing the 2FA secret at all.
