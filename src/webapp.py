"""
Local control panel for the muni-packet-crawler pipeline: a dashboard to
start/stop the crawl engine, and a browser for the municipalities/meetings/
packets tables with the ability to mark packets reviewed, force a
re-crawl, or manually add/delete a municipality.

Run with: python src/webapp.py, then open http://localhost:5000
"""

import subprocess
import sys
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crawl_sites import BOARD_KEYWORDS
from db import get_connection

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = BASE_DIR / "data" / "crawl_engine.log"
PAGE_SIZE = 50

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = "local-only-control-panel"

_engine_proc: subprocess.Popen | None = None
_engine_desc: str = ""


def dict_rows(cur) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_all_states() -> list[str]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT state FROM municipalities ORDER BY state")
    states = [r[0] for r in cur.fetchall()]
    conn.close()
    return states


def is_engine_running() -> bool:
    return _engine_proc is not None and _engine_proc.poll() is None


@app.route("/")
def dashboard():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT site_status, COUNT(*) FROM municipalities GROUP BY site_status ORDER BY 2 DESC")
    site_status_counts = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM packets")
    total_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE is_upcoming = 1")
    upcoming_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE is_upcoming IS NULL")
    unknown_date_packets = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM packets WHERE reviewed = 1")
    reviewed_packets = cur.fetchone()[0]

    cur.execute(
        """
        SELECT state,
               COUNT(*) AS total,
               SUM(CASE WHEN site_status = 'ok' THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN site_status = 'no_packets_found' THEN 1 ELSE 0 END) AS no_packets_found,
               SUM(CASE WHEN site_status = 'no_agenda_found' THEN 1 ELSE 0 END) AS no_agenda_found,
               SUM(CASE WHEN site_status = 'error' THEN 1 ELSE 0 END) AS error,
               SUM(CASE WHEN site_status = 'unchecked' THEN 1 ELSE 0 END) AS unchecked
        FROM municipalities GROUP BY state ORDER BY total DESC
        """
    )
    state_table = dict_rows(cur)
    conn.close()

    log_tail = ""
    if LOG_PATH.exists():
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        log_tail = "\n".join(lines[-40:])

    return render_template(
        "dashboard.html",
        site_status_counts=site_status_counts,
        total_packets=total_packets,
        upcoming_packets=upcoming_packets,
        unknown_date_packets=unknown_date_packets,
        reviewed_packets=reviewed_packets,
        state_table=state_table,
        all_states=get_all_states(),
        engine_running=is_engine_running(),
        engine_pid=_engine_proc.pid if _engine_proc else None,
        engine_state=_engine_desc,
        log_tail=log_tail,
    )


@app.route("/engine/start", methods=["POST"])
def engine_start():
    global _engine_proc, _engine_desc
    if is_engine_running():
        flash("Engine is already running.")
        return redirect(url_for("dashboard"))

    args = [sys.executable, str(Path(__file__).resolve().parent / "crawl_sites.py")]
    state = request.form.get("state", "").strip()
    limit = request.form.get("limit", "").strip()
    recheck = request.form.get("recheck") == "1"
    if state:
        args += ["--state", state]
    if limit:
        args += ["--limit", limit]
    if recheck:
        args += ["--recheck"]

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "w", encoding="utf-8")
    _engine_proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, cwd=str(BASE_DIR))
    _engine_desc = (state or "all states") + (f", limit {limit}" if limit else "") + (", recheck" if recheck else "")
    flash(f"Started crawl ({_engine_desc}).")
    return redirect(url_for("dashboard"))


@app.route("/engine/stop", methods=["POST"])
def engine_stop():
    global _engine_proc
    if is_engine_running():
        _engine_proc.terminate()
        flash("Engine stopped.")
    else:
        flash("Engine was not running.")
    return redirect(url_for("dashboard"))


@app.route("/municipalities")
def municipalities():
    state = request.args.get("state", "")
    status = request.args.get("status", "")
    q = request.args.get("q", "")
    page = max(1, request.args.get("page", 1, type=int))

    conn = get_connection()
    cur = conn.cursor()

    where = []
    params: list = []
    if state:
        where.append("state = ?")
        params.append(state)
    if status:
        where.append("site_status = ?")
        params.append(status)
    if q:
        where.append("name LIKE ?")
        params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    cur.execute(f"SELECT COUNT(*) FROM municipalities {where_sql}", params)
    total = cur.fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    cur.execute(
        f"SELECT * FROM municipalities {where_sql} ORDER BY state, name LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    rows = dict_rows(cur)
    conn.close()

    return render_template(
        "municipalities.html",
        rows=rows,
        all_states=get_all_states(),
        all_statuses=["unchecked", "ok", "no_packets_found", "no_agenda_found", "error"],
        state=state,
        status=status,
        q=q,
        page=page,
        total=total,
        total_pages=total_pages,
    )


@app.route("/municipalities/new", methods=["GET", "POST"])
def new_municipality():
    if request.method == "POST":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO municipalities (name, state, place_type, official_url, source)
            VALUES (?, ?, ?, ?, 'manual')
            ON CONFLICT(state, name) DO UPDATE SET official_url = excluded.official_url
            """,
            (request.form["name"].strip(), request.form["state"].strip(),
             request.form["place_type"], request.form["official_url"].strip()),
        )
        conn.commit()
        conn.close()
        flash(f"Added {request.form['name']}, {request.form['state']}.")
        return redirect(url_for("municipalities"))
    return render_template("new_municipality.html")


@app.route("/municipalities/<int:muni_id>")
def municipality_detail(muni_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM municipalities WHERE id = ?", (muni_id,))
    m = dict_rows(cur)
    if not m:
        conn.close()
        flash("Municipality not found.")
        return redirect(url_for("municipalities"))
    m = m[0]

    cur.execute(
        "SELECT * FROM packets WHERE municipality_id = ? ORDER BY meeting_date DESC",
        (muni_id,),
    )
    packet_rows = dict_rows(cur)
    conn.close()

    return render_template("municipality_detail.html", m=m, packets=packet_rows)


@app.route("/municipalities/<int:muni_id>/recheck", methods=["POST"])
def municipality_recheck(muni_id: int):
    conn = get_connection()
    conn.execute(
        "UPDATE municipalities SET site_status = 'unchecked', agenda_url = NULL, platform = NULL WHERE id = ?",
        (muni_id,),
    )
    conn.commit()
    conn.close()
    flash("Marked for re-check on next crawl.")
    return redirect(url_for("municipality_detail", muni_id=muni_id))


@app.route("/municipalities/<int:muni_id>/delete", methods=["POST"])
def municipality_delete(muni_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM meetings WHERE municipality_id = ?", (muni_id,))
    meeting_ids = [r[0] for r in cur.fetchall()]
    for mid in meeting_ids:
        cur.execute("DELETE FROM packets WHERE meeting_id = ?", (mid,))
    cur.execute("DELETE FROM meetings WHERE municipality_id = ?", (muni_id,))
    cur.execute("DELETE FROM municipalities WHERE id = ?", (muni_id,))
    conn.commit()
    conn.close()
    flash("Deleted.")
    return redirect(url_for("municipalities"))


@app.route("/packets")
def packets():
    state = request.args.get("state", "")
    which = request.args.get("which", "upcoming")
    reviewed = request.args.get("reviewed", "")
    board_type = request.args.get("board_type", "")
    page = max(1, request.args.get("page", 1, type=int))

    where = []
    params: list = []
    if which == "upcoming":
        where.append("p.is_upcoming = 1")
    elif which == "unknown":
        where.append("p.is_upcoming IS NULL")
    # "all" -> no extra filter beyond the save-time exclusion of confirmed-past packets
    if state:
        where.append("m.state = ?")
        params.append(state)
    if reviewed in ("0", "1"):
        where.append("p.reviewed = ?")
        params.append(int(reviewed))
    if board_type:
        where.append("p.board_type = ?")
        params.append(board_type)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT COUNT(*) FROM packets p
        JOIN municipalities m ON p.municipality_id = m.id
        {where_sql}
        """,
        params,
    )
    total = cur.fetchone()[0]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    cur.execute(
        f"""
        SELECT p.*, m.name, m.state FROM packets p
        JOIN municipalities m ON p.municipality_id = m.id
        {where_sql}
        ORDER BY p.meeting_date IS NULL, p.meeting_date
        LIMIT ? OFFSET ?
        """,
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    )
    rows = dict_rows(cur)
    conn.close()

    return render_template(
        "packets.html",
        rows=rows,
        all_states=get_all_states(),
        all_board_types=BOARD_KEYWORDS,
        state=state,
        which=which,
        reviewed=reviewed,
        board_type=board_type,
        page=page,
        total=total,
        total_pages=total_pages,
    )


@app.route("/packets/<int:packet_id>/toggle_reviewed", methods=["POST"])
def toggle_reviewed(packet_id: int):
    conn = get_connection()
    conn.execute("UPDATE packets SET reviewed = 1 - reviewed WHERE id = ?", (packet_id,))
    conn.commit()
    conn.close()
    next_url = request.form.get("next") or url_for("packets")
    return redirect(next_url)


if __name__ == "__main__":
    app.run(debug=False, port=5000)
