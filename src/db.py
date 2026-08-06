"""SQLite schema and connection helper for the municipality/meeting/packet pipeline."""

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "muni.db"

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
    not the crawl's own (much lighter) per-municipality writes."""
    global _setup_done
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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
