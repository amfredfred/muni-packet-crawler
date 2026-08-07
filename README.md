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

A general-purpose business invoice tool sharing the same webapp/database,
unrelated to the crawler itself:

- **Business profile** (`/business-profile`) — default letterhead (name,
  address, phone, email, payment/account details, theme color, logo),
  overridable per invoice.
- **Commission types** (`/commission-types`) — reusable fee templates. Each
  has any number of line-item rules (percentage-of-base or fixed fee)
  evaluated against a base amount to produce a starting set of line items —
  never the invoice's final total, which is always the live sum of its own
  (freely editable) line items.
- **Invoices** (`/invoices`) — a single JSON textarea is the entire editing
  surface (pre-filled with sensible defaults/current values), plus a live
  preview pane showing the actual generated PDF, refreshing as you type.
  `invoice_number` is part of the JSON and overridable — leave it blank to
  auto-generate (new) or keep the current one (editing). Per-invoice logo
  upload falls back to the business profile's logo when not set. PDF
  rendering via reportlab: fitted header logo, faint background watermark,
  configurable accent color.

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
