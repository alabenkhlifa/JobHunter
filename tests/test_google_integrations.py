import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobhunter_integrations import gmail_watcher, google_tracker


def test_tracker_formats_dates_and_status_colors():
    assert google_tracker.format_dt("2026-07-18T14:47:00+00:00").endswith("15:47") or google_tracker.format_dt("2026-07-18T14:47:00+00:00").endswith("14:47")
    assert google_tracker.sheet_text_dt("2026-07-18T15:47:00+00:00").startswith("'")
    assert google_tracker.status_color("submitted")["green"] > google_tracker.status_color("submitted")["red"]
    assert google_tracker.status_color("blocked_login_required")["red"] > google_tracker.status_color("blocked_login_required")["green"]


def test_tracker_rows_include_resume_cover_and_screenshot_paths_without_drive(tmp_path: Path):
    repo = tmp_path
    output = repo / "data" / "output" / "job-1"
    evidence = output / "evidence"
    evidence.mkdir(parents=True)
    (output / "Resume_Test.pdf").write_text("resume")
    (output / "CoverLetter_Test.pdf").write_text("cover")
    shot = evidence / "submitted.png"
    shot.write_text("png")

    db = repo / "data" / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT, title TEXT, company TEXT, url TEXT)")
    conn.execute("""
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, stage TEXT, package_path TEXT,
            created_at TEXT, approved_at TEXT, submitted_at TEXT, platform TEXT, application_url TEXT,
            notes TEXT, error TEXT, application_type TEXT, evidence_path TEXT
        )
    """)
    conn.execute("INSERT INTO jobs VALUES ('job-1','Backend Engineer','ExampleCo','https://example.com/job')")
    conn.execute(
        "INSERT INTO applications (job_id,stage,package_path,created_at,submitted_at,platform,application_url,evidence_path) VALUES (?,?,?,?,?,?,?,?)",
        ('job-1','submitted','data/output/job-1','2026-07-18T15:47:00+00:00','2026-07-18T15:47:00+00:00','ATS','https://example.com/app',str(shot)),
    )
    conn.commit(); conn.close()

    rows = google_tracker.rows_from_db(db, repo, drive=None)
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == "submitted"
    assert row[8].endswith("Resume_Test.pdf")
    assert row[9].endswith("CoverLetter_Test.pdf")
    assert row[11].endswith("submitted.png")


def test_gmail_watcher_relevance_uses_keywords_and_job_terms():
    jobs = [{"title": "Solutions Architect SkyCargo", "company": "Emirates"}]
    relevant, reasons = gmail_watcher.is_relevant("Thank you for your application to Emirates SkyCargo", jobs)
    assert relevant
    assert reasons

    noisy, _ = gmail_watcher.is_relevant("Weekly newsletter unsubscribe promotion", jobs)
    assert not noisy
