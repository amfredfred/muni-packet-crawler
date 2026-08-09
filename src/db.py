"""SQLite schema and connection helper for the municipality/meeting/packet pipeline."""

import shutil
import sqlite3
import sys
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "muni.db"
DATA_DIR = DB_PATH.parent

# A fresh deployment (e.g. a VPS clone) has no muni.db yet — only the
# committed snapshot of the static tables (see export_seed_db.py). muni.db
# itself is never committed since it also holds the live TOTP secret.
SEED_DB_PATH = DATA_DIR / "muni_seed.db"


def redirect_console_if_headless(log_filename: str) -> None:
    """Under pythonw.exe (no console attached — used so Task Scheduler can
    run this with no visible terminal window), sys.stdout/sys.stderr are
    None, not just discarded. Any bare print() would raise AttributeError
    and crash the process instantly on the very first line of output.
    Call this at the very start of main() to redirect to a log file
    instead; under a normal python.exe console, sys.stdout is a real
    stream and this is a no-op."""
    if sys.stdout is not None:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(DATA_DIR / log_filename, "a", buffering=1, encoding="utf-8")
    sys.stdout = log_file
    sys.stderr = log_file

SCHEMA = """
CREATE TABLE IF NOT EXISTS municipalities (
    id INTEGER PRIMARY KEY,
    wikidata_id TEXT UNIQUE,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    place_type TEXT NOT NULL,
    official_url TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'wikidata',
    agenda_url TEXT,
    platform TEXT,
    site_status TEXT DEFAULT 'unchecked',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_municipalities_state ON municipalities(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_municipalities_state_name ON municipalities(state, name);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,
    municipality_id INTEGER NOT NULL REFERENCES municipalities(id),
    board_name TEXT,
    meeting_date TEXT,
    source_url TEXT NOT NULL,
    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(municipality_id, source_url)
);

CREATE TABLE IF NOT EXISTS packets (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    url TEXT NOT NULL,
    title TEXT,
    match_reason TEXT,
    found_via TEXT,
    discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reviewed INTEGER DEFAULT 0,
    UNIQUE(meeting_id, url)
);

-- Single-row table: the CHECK makes a second row impossible outright, so
-- "registered" is simply "does this one row exist" — no separate flag to
-- get out of sync. To allow a new registration, delete the row directly
-- (sqlite3 data/muni.db "DELETE FROM auth_totp") and the webapp falls back
-- to showing the setup page again on the next visit.
CREATE TABLE IF NOT EXISTS auth_totp (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    secret TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- State-directory sources that were blocked, dead, or otherwise not
-- scrapable when last checked (see README's "Checked and ruled out"
-- section for the full research). last_status lets the dashboard's retry
-- button re-test a specific URL later without re-deriving this list from
-- scratch — many of these are bot-blocks or outages that could resolve on
-- their own over time.
CREATE TABLE IF NOT EXISTS blocked_sources (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    url TEXT NOT NULL,
    reason TEXT NOT NULL,
    category TEXT NOT NULL,
    last_checked_at TEXT,
    last_status TEXT DEFAULT 'unknown',
    UNIQUE(state, url)
);

"""

# Added after the initial schema — kept as migrations rather than folded into
# CREATE TABLE so existing databases pick them up without a manual rebuild.
MIGRATIONS = [
    ("packets", "meeting_date", "ALTER TABLE packets ADD COLUMN meeting_date TEXT"),
    ("packets", "is_upcoming", "ALTER TABLE packets ADD COLUMN is_upcoming INTEGER"),
    # Same PDF can be linked from more than one listing page for the same
    # municipality (e.g. both a "Planning Board" page and a general
    # "Agendas" page); the old UNIQUE(meeting_id, url) only deduped within
    # a single listing page, so the same packet could appear once per page
    # that referenced it. municipality_id + a real dedup index fixes that.
    ("packets", "municipality_id", "ALTER TABLE packets ADD COLUMN municipality_id INTEGER"),
    ("packets", "board_type", "ALTER TABLE packets ADD COLUMN board_type TEXT"),
    # PDF content review (see pdf_review.py / review_packets.py): NULL
    # review_method means not yet reviewed. priority=1 is the actionable
    # signal — a packet whose PDF names a specific applicant/owner (e.g. a
    # liquor-license application), surfaced for follow-up ahead of routine
    # agendas. This is a best-effort text/OCR heuristic, not a verified fact.
    ("packets", "review_method", "ALTER TABLE packets ADD COLUMN review_method TEXT"),
    ("packets", "applicant_snippet", "ALTER TABLE packets ADD COLUMN applicant_snippet TEXT"),
    ("packets", "priority", "ALTER TABLE packets ADD COLUMN priority INTEGER DEFAULT 0"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(ddl)

    conn.execute(
        """
        UPDATE packets SET municipality_id = (
            SELECT municipality_id FROM meetings WHERE meetings.id = packets.meeting_id
        )
        WHERE municipality_id IS NULL
        """
    )
    # Collapse any duplicates that already exist under the old, narrower
    # dedup key before the new unique index can be created.
    conn.execute(
        """
        DELETE FROM packets WHERE id NOT IN (
            SELECT MIN(id) FROM packets GROUP BY municipality_id, url
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_packets_municipality_url ON packets(municipality_id, url)"
    )
    conn.commit()


_setup_lock = threading.Lock()
_setup_done = False


def _seed_from_snapshot_if_missing() -> None:
    if not DB_PATH.exists() and SEED_DB_PATH.exists():
        shutil.copy2(SEED_DB_PATH, DB_PATH)


def get_connection() -> sqlite3.Connection:
    """Each caller should get its own connection — sqlite3 connections
    aren't safely shared across threads. WAL mode lets concurrent readers
    coexist with a writer instead of erroring immediately, and the
    busy_timeout makes a thread wait out a brief writer conflict (e.g. two
    crawl workers finishing at the same moment) rather than raising
    'database is locked'.

    Schema creation + migrations only need to run once per process — they
    used to run on every call, which under a concurrent crawl meant every
    worker thread was re-running a full-table UPDATE and DELETE (the
    municipality_id backfill and dedup cleanup) on every single
    municipality processed. That constant redundant write traffic was the
    actual cause of 'database is locked' errors once workers were added,
    not the crawl's own (much lighter) per-municipality writes.

    The seed-from-snapshot check has to happen here too (before connect()
    creates an empty muni.db just by opening a path that doesn't exist yet)
    rather than as a separate setup script — this way every entry point
    (webapp, crawl, review, build_index) gets a pre-populated database on
    a fresh deployment automatically, with no manual step to forget."""
    global _setup_done
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _setup_done:
        with _setup_lock:
            if not _setup_done:
                _seed_from_snapshot_if_missing()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    if not _setup_done:
        with _setup_lock:
            if not _setup_done:
                conn.executescript(SCHEMA)
                _apply_migrations(conn)
                _setup_done = True
    return conn
