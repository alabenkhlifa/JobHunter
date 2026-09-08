"""Recompute stored scores for unsent jobs with status 'new'.

Run with --dry-run (the default) to preview, or --apply to update scores
after a verified SQLite backup. Backups are unique files in data/backups.
The send path still applies its own current freshness and eligibility checks.

Freshness stays pinned to date_scraped so scoring-rule changes are isolated
from elapsed time. Later recruiter/credibility enrichment is excluded from
scoring, matching the original collection inputs, but remains stored intact.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import job_scoring
import scraper

DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

UNSCORED_AT_COLLECTION_TIME = {"recruiter_company": "", "credibility_notes": ""}


def rescore(job, now):
    """Mirrors scraper.score_job exactly, with `now` pinned instead of live."""
    result = job_scoring.evaluate(
        job,
        allowed_locations=tuple(loc.lower() for loc in scraper.CONFIG.get("allowed_locations", ())),
        max_experience=scraper.CONFIG.get("max_experience", 8),
        now=now,
    )
    if result["reason"]:
        return 0, [f"knocked out: {result['reason']}"]
    breakdown = [
        f"{name} {result['parts'][name]:.2f}x{job_scoring.WEIGHTS[name]}"
        for name in job_scoring.WEIGHTS
    ]
    breakdown.append(f"band {result['band']}")
    return result["total"], breakdown


def create_backup(db_path, backup_dir):
    """Create a private, unique, verified snapshot, including committed WAL data."""
    db_path = Path(db_path).resolve(strict=True)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    with tempfile.NamedTemporaryFile(
        prefix=f"jobs-before-rescore-{stamp}-", suffix=".sqlite3",
        dir=backup_dir, delete=False,
    ) as reserved:
        backup_path = Path(reserved.name)
    try:
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("Database backup failed its integrity check")
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def run_rescore(db_path, *, apply=False, backup_dir=None):
    """Preview or atomically apply score updates without changing other job fields."""
    db_path = Path(db_path).resolve(strict=True)
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(db_path.as_uri() + f"?mode={mode}", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            # Block competing writers while planning, backing up, and applying.
            conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            rows = conn.execute(
                "SELECT * FROM jobs WHERE notified = 0 AND status = 'new'"
            ).fetchall()
            updates = []
            summary = {
                "applied": apply, "pending_rows": len(rows), "affected_rows": 0,
                "score_changes": 0, "breakdown_only_changes": 0,
                "crossed_above": 0, "crossed_below": 0,
                "cutoff": job_scoring.SEND_CUTOFF, "backup_path": None,
            }
            for row in rows:
                job = dict(row, **UNSCORED_AT_COLLECTION_TIME)
                try:
                    now = datetime.fromisoformat(job["date_scraped"].replace("Z", "+00:00"))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "Pending job has an invalid collection timestamp; no scores were changed"
                    ) from exc
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                new_score, breakdown = rescore(job, now)
                breakdown_text = ", ".join(breakdown)
                if new_score == row["score"] and breakdown_text == row["score_breakdown"]:
                    continue
                updates.append((new_score, breakdown_text, row["id"]))
                summary["score_changes" if new_score != row["score"] else "breakdown_only_changes"] += 1
                old_score = row["score"] or 0
                if old_score < job_scoring.SEND_CUTOFF <= new_score:
                    summary["crossed_above"] += 1
                elif new_score < job_scoring.SEND_CUTOFF <= old_score:
                    summary["crossed_below"] += 1
            summary["affected_rows"] = len(updates)
            if apply and updates:
                # Use a separate reader: backing up the active write connection
                # itself would wait for its own transaction to finish.
                backup_path = create_backup(db_path, backup_dir or db_path.parent / "backups")
                summary["backup_path"] = str(backup_path)
                changed = conn.executemany(
                    "UPDATE jobs SET score = ?, score_breakdown = ? "
                    "WHERE id = ? AND notified = 0 AND status = 'new'",
                    updates,
                ).rowcount
                if changed != len(updates):
                    raise RuntimeError("Pending jobs changed during rescoring; updates rolled back")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--backup-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Back up and commit score updates")
    mode.add_argument("--dry-run", action="store_true", help="Preview without writing (default)")
    args = parser.parse_args(argv)
    try:
        summary = run_rescore(args.db, apply=args.apply, backup_dir=args.backup_dir)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
