"""Autopilot run state: one ``auto_apply_runs`` row per local day, one log row per job.

The ``applications`` table stays the Sheet's mirror; everything the autopilot
needs to stay idempotent across retries lives here instead.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import policy as policy_module

RUNS_DDL = """CREATE TABLE IF NOT EXISTS auto_apply_runs (run_id TEXT PRIMARY KEY, review_finished_at TEXT, plan_finished_at TEXT, summary_json TEXT, summary_sent_at TEXT)"""
LOG_DDL = """CREATE TABLE IF NOT EXISTS auto_apply_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, job_id TEXT NOT NULL,
  status TEXT NOT NULL, criteria_json TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  telegram_notified_at TEXT, UNIQUE(run_id, job_id))"""

# Application stages at or after the submit click, or his own later decisions.
# The one definition the gate, the engine and selection all consult.
POST_SUBMISSION_STAGES = frozenset({
    "submission_attempted", "submitted", "approved", "application_progressed", "action_required",
    "assessment_requested", "interview", "interview_invited", "offer", "offer_received", "hired",
    "rejected", "closed", "withdrawn",
})


def already_submitted(conn, job_id):
    """True when any application row for the job reached a submit click or later.

    Reads only; safe inside the gate's ``BEGIN IMMEDIATE`` transaction.
    """
    if not table_exists(conn, "applications"):
        return False
    for stage, submitted_at in conn.execute(
            "SELECT stage, submitted_at FROM applications WHERE job_id=?", (str(job_id),)):
        if submitted_at or str(stage or "") in POST_SUBMISSION_STAGES:
            return True
    return False


STATUSES = ("selected", "packaged", "applying", "submitted", "unverified", "blocked",
            "deferred", "closed", "handed_off", "rehearsed")
CAP_STATUSES = ("applying", "submitted", "unverified")
# Rows a later plan run of the same day carries forward instead of re-selecting.
ACTIVE_STATUSES = ("packaged", "applying")
# Rows a later plan run of the same day may reconsider.
RETRY_STATUSES = ("selected", "deferred")
SETTLED_STATUSES = ("submitted", "unverified", "blocked", "handed_off", "rehearsed", "closed")

LOCK_NAME = "autopilot.lock"


def now_utc():
    return datetime.now(timezone.utc)


def iso(when=None):
    return (when or now_utc()).astimezone(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn):
    conn.execute(RUNS_DDL)
    conn.execute(LOG_DDL)
    conn.commit()


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def run_id_for(now=None, tz="Africa/Tunis"):
    """The local calendar date in the policy timezone: one run, one cap, per day."""
    now = now or now_utc()
    if now.tzinfo is None:
        raise ValueError("run_id_for needs a timezone-aware time")
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    return now.astimezone(zone).date().isoformat()


def _row_dict(cursor, row):
    if row is None:
        return None
    names = [column[0] for column in cursor.description]
    item = dict(zip(names, row))
    try:
        item["criteria"] = json.loads(item.get("criteria_json") or "[]")
    except ValueError:
        item["criteria"] = []
    return item


def get_log(conn, run_id, job_id):
    if not table_exists(conn, "auto_apply_log"):
        return None
    cursor = conn.execute("SELECT * FROM auto_apply_log WHERE run_id=? AND job_id=?", (run_id, job_id))
    return _row_dict(cursor, cursor.fetchone())


def log_rows(conn, run_id, statuses=None):
    if not table_exists(conn, "auto_apply_log"):
        return []
    sql, params = "SELECT * FROM auto_apply_log WHERE run_id=?", [run_id]
    if statuses:
        sql += f" AND status IN ({','.join('?' * len(statuses))})"
        params.extend(statuses)
    cursor = conn.execute(sql + " ORDER BY id", params)
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def upsert_log(conn, run_id, job_id, status, *, criteria=None, reason=None, now=None, commit=True):
    """Insert or move one job's row for this run; criteria are kept when not given."""
    if status not in STATUSES:
        raise ValueError(f"unknown autopilot log status {status!r}")
    stamp = iso(now)
    criteria_json = json.dumps(list(criteria), ensure_ascii=False) if criteria is not None else None
    conn.execute(
        """INSERT INTO auto_apply_log (run_id, job_id, status, criteria_json, reason, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id, job_id) DO UPDATE SET status=excluded.status,
             criteria_json=COALESCE(?, auto_apply_log.criteria_json),
             reason=excluded.reason, updated_at=excluded.updated_at""",
        (run_id, job_id, status, criteria_json or "[]", reason, stamp, stamp, criteria_json),
    )
    if commit:
        conn.commit()


def mark_notified(conn, run_id, job_id, *, now=None, commit=True):
    conn.execute("UPDATE auto_apply_log SET telegram_notified_at=? WHERE run_id=? AND job_id=?",
                 (iso(now), run_id, job_id))
    if commit:
        conn.commit()


def cap_used_today(conn, run_id, *, include_rehearsed=False):
    """Applications started or done today; rehearsals count only when asked."""
    if not table_exists(conn, "auto_apply_log"):
        return 0
    statuses = CAP_STATUSES + (("rehearsed",) if include_rehearsed else ())
    return conn.execute(
        f"SELECT COUNT(*) FROM auto_apply_log WHERE run_id=? AND status IN ({','.join('?' * len(statuses))})",
        (run_id, *statuses),
    ).fetchone()[0]


def count_status(conn, run_id, status):
    if not table_exists(conn, "auto_apply_log"):
        return 0
    return conn.execute("SELECT COUNT(*) FROM auto_apply_log WHERE run_id=? AND status=?",
                        (run_id, status)).fetchone()[0]


def get_run(conn, run_id):
    if not table_exists(conn, "auto_apply_runs"):
        return None
    cursor = conn.execute("SELECT * FROM auto_apply_runs WHERE run_id=?", (run_id,))
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description], row)) if row else None


_RUN_FIELDS = ("review_finished_at", "plan_finished_at", "summary_json", "summary_sent_at")


def update_run(conn, run_id, *, commit=True, **fields):
    unknown = set(fields) - set(_RUN_FIELDS)
    if unknown:
        raise ValueError(f"unknown run fields: {sorted(unknown)}")
    conn.execute("INSERT INTO auto_apply_runs (run_id) VALUES (?) ON CONFLICT(run_id) DO NOTHING", (run_id,))
    for name, value in fields.items():
        conn.execute(f"UPDATE auto_apply_runs SET {name}=? WHERE run_id=?", (value, run_id))
    if commit:
        conn.commit()


def lock_path():
    return policy_module.state_dir() / LOCK_NAME


@contextlib.contextmanager
def autopilot_lock(path=None):
    """Yield True when this process holds the run lock, False when another does."""
    path = Path(path) if path else lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def connect(db_path, *, read_only=False, timeout=30):
    path = Path(db_path).expanduser().resolve()
    if read_only:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=timeout)
    else:
        if not path.exists():
            raise FileNotFoundError(f"database not found: {path}")
        conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn
