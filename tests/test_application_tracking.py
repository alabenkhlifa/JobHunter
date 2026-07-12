import sqlite3
from datetime import datetime, timezone

import scraper


def make_conn_with_jobs():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            source TEXT,
            score INTEGER,
            date_posted TEXT,
            date_scraped TEXT,
            notified INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            tech_required TEXT DEFAULT '',
            tech_nice_to_have TEXT DEFAULT '',
            min_experience INTEGER DEFAULT -1,
            salary TEXT DEFAULT '',
            work_model TEXT DEFAULT 'on-site',
            score_breakdown TEXT DEFAULT '',
            status TEXT DEFAULT 'new'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jobs (id, title, company, location, url, source, score, date_posted, date_scraped)
        VALUES ('li-1', 'Lead Backend Engineer', 'ExampleCo', 'Dubai', 'https://example.com', 'LinkedIn', 25, '', '')
        """
    )
    conn.commit()
    return conn


def test_init_application_tracking_creates_expected_table():
    conn = make_conn_with_jobs()

    scraper.init_application_tracking(conn)

    columns = [row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()]
    assert columns == [
        "id",
        "job_id",
        "stage",
        "package_path",
        "created_at",
        "approved_at",
        "submitted_at",
        "platform",
        "application_type",
        "application_url",
        "evidence_path",
        "notes",
        "error",
    ]
    answer_columns = [row[1] for row in conn.execute("PRAGMA table_info(application_answers)").fetchall()]
    assert answer_columns == [
        "id",
        "question_key",
        "question_text",
        "answer",
        "answer_source",
        "confirmed",
        "created_at",
        "updated_at",
    ]


def test_record_application_stage_inserts_then_updates_latest_state():
    conn = make_conn_with_jobs()
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)

    first = scraper.record_application_stage(
        conn,
        "li-1",
        "interested",
        notes="User tapped Interested",
        now=now,
    )
    second = scraper.record_application_stage(
        conn,
        "li-1",
        "package_generated",
        package_path="data/output/package-li-1",
        platform="LinkedIn",
        application_type="easy_apply",
        application_url="https://example.com/apply",
        evidence_path="data/output/package-li-1/draft.png",
        notes="Resume and cover letter ready",
        now=now,
    )

    assert first == second
    row = conn.execute(
        """
        SELECT job_id, stage, package_path, platform, application_type, application_url, evidence_path, notes, created_at
        FROM applications WHERE job_id = 'li-1'
        """
    ).fetchone()
    assert row == (
        "li-1",
        "package_generated",
        "data/output/package-li-1",
        "LinkedIn",
        "easy_apply",
        "https://example.com/apply",
        "data/output/package-li-1/draft.png",
        "Resume and cover letter ready",
        "2026-07-02T12:00:00+00:00",
    )


def test_mark_interested_records_application_stage():
    conn = make_conn_with_jobs()

    updated = scraper.mark_interested(conn, "li-1")

    assert updated is True
    assert conn.execute("SELECT status FROM jobs WHERE id = 'li-1'").fetchone()[0] == "interested"
    application = conn.execute(
        "SELECT job_id, stage, platform, application_type, application_url, notes FROM applications WHERE job_id = 'li-1'"
    ).fetchone()
    assert application == (
        "li-1",
        "interested",
        "LinkedIn",
        "linkedin_unknown",
        "https://example.com",
        "Marked interested from JobHunter; next safe step is package_generated then draft_ready before approval/submission.",
    )


def test_mark_interested_missing_job_does_not_create_application():
    conn = make_conn_with_jobs()

    updated = scraper.mark_interested(conn, "missing")

    assert updated is False
    scraper.init_application_tracking(conn)
    count = conn.execute("SELECT COUNT(*) FROM applications WHERE job_id = 'missing'").fetchone()[0]
    assert count == 0


def test_record_application_stage_auto_syncs_when_enabled_for_file_db(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            source TEXT,
            score INTEGER,
            date_posted TEXT,
            date_scraped TEXT,
            notified INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            tech_required TEXT DEFAULT '',
            tech_nice_to_have TEXT DEFAULT '',
            min_experience INTEGER DEFAULT -1,
            salary TEXT DEFAULT '',
            work_model TEXT DEFAULT 'on-site',
            score_breakdown TEXT DEFAULT '',
            status TEXT DEFAULT 'new'
        )
        """
    )
    calls = []

    class Result:
        returncode = 0
        stdout = "synced"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setenv("JOBHUNTER_AUTO_SYNC_TRACKER", "true")
    monkeypatch.setenv("JOBHUNTER_TRACKER_SYNC_COMMAND", "/tmp/sync-tracker --fast")
    monkeypatch.setattr(scraper.subprocess, "run", fake_run)

    scraper.record_application_stage(conn, "li-1", "package_generated")

    assert calls
    assert calls[0][0] == ["/tmp/sync-tracker", "--fast"]
    assert calls[0][1]["check"] is False


def test_record_application_stage_auto_sync_failure_does_not_block(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)

    def fake_run(command, **kwargs):
        raise TimeoutError("slow tracker")

    monkeypatch.setenv("JOBHUNTER_AUTO_SYNC_TRACKER", "true")
    monkeypatch.setenv("JOBHUNTER_TRACKER_SYNC_COMMAND", "/tmp/sync-tracker")
    monkeypatch.setattr(scraper.subprocess, "run", fake_run)

    app_id = scraper.record_application_stage(conn, "li-1", "blocked_captcha")

    assert isinstance(app_id, int)
    assert conn.execute("SELECT stage FROM applications WHERE job_id = 'li-1'").fetchone()[0] == "blocked_captcha"
