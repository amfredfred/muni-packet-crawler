"""
Exports the genuinely static, safe-to-ship part of muni.db — municipalities,
meetings, packets, blocked_sources — into a separate committed file,
data/muni_seed.db. This is what a fresh deployment (e.g. a VPS) should
start from, rather than copying muni.db itself: muni.db also holds the
live TOTP secret (auth_totp, once 2FA setup completes), which should never
be shared between deployments or committed to git.

Re-run this after any crawl/review pass to refresh the committed seed
with the latest scraped data.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DB_PATH

SEED_TABLES = ["municipalities", "meetings", "packets", "blocked_sources", "opened_links"]
SEED_DB_PATH = DB_PATH.parent / "muni_seed.db"


def main() -> None:
    if SEED_DB_PATH.exists():
        SEED_DB_PATH.unlink()

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(SEED_DB_PATH)

    for table in SEED_TABLES:
        create_sql = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not create_sql:
            print(f"  skipping {table}: not present in {DB_PATH.name}")
            continue
        dst.execute(create_sql[0])

        index_rows = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
        for (index_sql,) in index_rows:
            dst.execute(index_sql)

        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            placeholders = ",".join("?" * len(rows[0]))
            dst.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
        print(f"  {table}: {len(rows)} rows")

    dst.commit()
    dst.close()
    src.close()
    print(f"done — wrote {SEED_DB_PATH}")


if __name__ == "__main__":
    main()
