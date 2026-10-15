"""Re-check that selected offers are still open before any work is spent on them.

Closed listings become ``unavailable`` (archived ones too, which
``jobhunter_availability.recordcheck`` leaves alone). A closed listing that
already has a tracker row goes through the listing monitor, which owns the
Sheet closure and the Telegram alert; it reuses this run's check instead of
fetching the listing again. ``unknown`` defers the job to the next run.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import jobhunter_availability

from . import store

WORKERS = 3
CLOSED_REASONS = ("closed_marker", "expired_valid_through")
REPO = Path(__file__).resolve().parent.parent


def _unknown(job, reason="request_failed"):
    return {"state": "unknown", "reason": reason, "verification_scope": "source_listing",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job_id": str(job.get("id") or ""), "url": "", "source_job_id": "", "matched": False}


def check_jobs(jobs, *, check=None, workers=WORKERS):
    """{job_id: result} for each job, checked three at a time; a crash reads as unknown."""
    check = check or jobhunter_availability.check

    def one(job):
        try:
            result = check(job)
        except Exception:  # noqa: BLE001 - one broken listing must not stop the run
            return job["id"], _unknown(job)
        return job["id"], result if isinstance(result, dict) else _unknown(job)

    if not jobs:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(pool.map(one, jobs))


def state_of(result):
    """open | closed | unknown, trusting only matched evidence."""
    state = result.get("state")
    if state == "closed" and result.get("matched") is True and result.get("reason") in CLOSED_REASONS:
        return "closed"
    if state == "open" and result.get("matched") is True:
        return "open"
    return "unknown"


def tracked(conn, job_id):
    if not store.table_exists(conn, "applications"):
        return False
    return conn.execute("SELECT 1 FROM applications WHERE job_id=? LIMIT 1", (job_id,)).fetchone() is not None


def record(conn, job, result, *, owned_interested=False):
    """Persist one check like recordcheck; returns the state actually trusted."""
    state = state_of(result)
    try:
        jobhunter_availability.recordcheck(conn, job, result)
    except ValueError:
        # Evidence for another listing, or the job row changed underneath us.
        return "unknown"
    if state == "closed":
        statuses = ("archived", "interested") if owned_interested else ("archived",)
        conn.execute(f"UPDATE jobs SET status='unavailable' WHERE id=? AND status IN "
                     f"({','.join('?' * len(statuses))})", (job["id"], *statuses))
    conn.commit()
    return state


def _tracker_args(db_path):
    """Listing-monitor arguments from the deployment .env; None when the tracker is not configured."""
    from dotenv import load_dotenv
    from jobhunter_integrations import google_tracker

    load_dotenv(REPO / ".env")
    if not os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID"):
        return None
    try:
        args = google_tracker.parse_args([])
    except SystemExit:
        return None
    args.db_path = Path(db_path).resolve()
    args.use_recorded_closures = False
    return args


def sync_tracked_closures(db_path, results, *, send, monitor=None, args=None):
    """Run the listing monitor once with this run's cached checks.

    Returns ``({job_id: sheet_synced}, note)``. Never raises: a monitor failure
    leaves the closure queued for the 17:00 listing check.
    """
    closed_ids = sorted(results)
    if not closed_ids:
        return {}, None
    if monitor is None:
        from jobhunter_integrations import listing_monitor
        monitor = listing_monitor.run_monitor
    args = args if args is not None else _tracker_args(db_path)
    if args is None:
        return {job_id: False for job_id in closed_ids}, "tracker not configured"

    def cached_check(job):
        cached = results.get(str(job.get("id")))
        return cached if cached is not None else jobhunter_availability.check(job)

    note = None
    try:
        outcome = monitor(args, send, check=cached_check)
        if isinstance(outcome, dict) and outcome.get("skipped_overlap"):
            note = "listing monitor busy; closure retried at the next listing check"
    except Exception as exc:  # noqa: BLE001 - provider errors can hold credentials
        note = f"listing monitor failed ({type(exc).__name__}); closure retried at the next listing check"
    synced = {}
    with closing(sqlite3.connect(str(Path(db_path).resolve()), timeout=30)) as conn:
        has_table = store.table_exists(conn, "tracker_listing_closures")
        for job_id in closed_ids:
            row = conn.execute(
                "SELECT tracker_synced_at FROM tracker_listing_closures WHERE job_id=? ORDER BY closed_at DESC LIMIT 1",
                (job_id,)).fetchone() if has_table else None
            synced[job_id] = bool(row and row[0])
    return synced, note
