import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobhunter_integrations import gmail_watcher, google_tracker


def test_tracker_formats_dates_and_status_colors():
    assert google_tracker.format_dt("2026-07-18T14:47:00+00:00").endswith("15:47") or google_tracker.format_dt("2026-07-18T14:47:00+00:00").endswith("14:47")
    assert google_tracker.sheet_text_dt("2026-07-18T15:47:00+00:00").startswith("'")
    assert google_tracker.status_color("submitted")["green"] > google_tracker.status_color("submitted")["red"]
    assert google_tracker.status_color("blocked_login_required")["red"] > google_tracker.status_color("blocked_login_required")["green"]
    assert google_tracker.status_color("rejected")["red"] > google_tracker.status_color("rejected")["green"]
    assert google_tracker.next_action("rejected", None) == "Application closed"


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


def test_gmail_watcher_ignores_google_sheet_share_mail_even_with_application_word():
    jobs = [{"title": "Backend Engineer", "company": "ExampleCo"}]
    text = (
        "Candidate User via Google Sheets drive-shares-dm-noreply@google.com "
        "Spreadsheet shared with you: Job Applications has invited you to edit "
        "the following spreadsheet: Job Applications"
    )
    relevant, reasons = gmail_watcher.is_relevant(text, jobs)
    assert not relevant
    assert reasons == []


def test_gmail_watcher_classifies_common_rejection_language():
    rejection_messages = [
        (
            "After careful consideration, we regret to inform you that we will not be "
            "progressing with your application."
        ),
        "Your application has not been successful.",
        "We won't be moving forward with your application.",
    ]
    for message in rejection_messages:
        outcome, reasons = gmail_watcher.classify_application_outcome(message)
        assert outcome == "rejected"
        assert reasons

    assert gmail_watcher.classify_application_outcome(
        "We received your application and will contact you about next steps."
    ) == (None, [])


def test_gmail_watcher_matches_one_submitted_application_by_exact_title():
    jobs = [
        {
            "id": "job-skycargo",
            "title": "Solutions Architect - SkyCargo",
            "company": "Emirates",
            "stage": "submitted",
        },
        {
            "id": "job-network",
            "title": "Solutions Architect - Network Operations",
            "company": "Emirates",
            "stage": "submitted",
        },
    ]

    job, reason = gmail_watcher.match_submitted_application(
        "Update for Solutions Architect - SkyCargo at Emirates", jobs
    )

    assert job["id"] == "job-skycargo"
    assert reason == "exact job title"


def test_gmail_watcher_does_not_match_ambiguous_or_unsubmitted_application():
    ambiguous = [
        {
            "id": "job-1",
            "title": "Backend Engineer",
            "company": "ExampleCo",
            "stage": "submitted",
        },
        {
            "id": "job-2",
            "title": "Backend Engineer",
            "company": "ExampleCo",
            "stage": "submitted",
        },
    ]
    job, reason = gmail_watcher.match_submitted_application(
        "Backend Engineer application at ExampleCo", ambiguous
    )
    assert job is None
    assert "multiple" in reason

    job, reason = gmail_watcher.match_submitted_application(
        "Backend Engineer application at ExampleCo",
        [{**ambiguous[0], "stage": "package_generated"}],
    )
    assert job is None
    assert "no unique" in reason


def test_gmail_watcher_records_rejection_and_syncs_tracker_once(tmp_path: Path, monkeypatch):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            url TEXT,
            date_scraped TEXT,
            status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            package_path TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            submitted_at TEXT,
            platform TEXT,
            application_type TEXT,
            application_url TEXT,
            evidence_path TEXT,
            notes TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
        (
            "job-skycargo",
            "Solutions Architect - SkyCargo",
            "Emirates",
            "https://example.com/job",
            "2026-07-01T00:00:00+00:00",
            "interested",
        ),
    )
    conn.execute(
        """
        INSERT INTO applications (
            job_id, stage, created_at, submitted_at, notes
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "job-skycargo",
            "submitted",
            "2026-07-10T15:21:41+00:00",
            "2026-07-10T15:21:41+00:00",
            "Submission confirmed.",
        ),
    )
    conn.commit()
    conn.close()

    sync_calls = []
    monkeypatch.setattr(
        "scraper.sync_application_tracker_if_enabled",
        lambda: sync_calls.append("sync") or True,
    )
    job = {
        "id": "job-skycargo",
        "title": "Solutions Architect - SkyCargo",
        "company": "Emirates",
        "stage": "submitted",
    }
    now = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)

    result = gmail_watcher.record_rejected_application(db, job, now=now)
    repeated = gmail_watcher.record_rejected_application(db, job, now=now)

    assert result == {
        "status": "updated",
        "reason": "application marked rejected",
        "tracker_synced": True,
    }
    assert repeated["status"] == "already_rejected"
    assert sync_calls == ["sync"]

    conn = sqlite3.connect(db)
    application = conn.execute(
        "SELECT stage, submitted_at, notes FROM applications WHERE job_id = ?",
        ("job-skycargo",),
    ).fetchone()
    job_status = conn.execute(
        "SELECT status FROM jobs WHERE id = ?",
        ("job-skycargo",),
    ).fetchone()[0]
    conn.close()

    assert application[0] == "rejected"
    assert application[1] == "2026-07-10T15:21:41+00:00"
    assert application[2].startswith("Submission confirmed. | Rejection detected by Gmail watcher")
    assert job_status == "rejected"


def test_gmail_watcher_formats_explicit_rejection_alert():
    alert = gmail_watcher.format_alert(
        [
            {
                "outcome": "rejected",
                "matched_job": {
                    "id": "job-skycargo",
                    "title": "Solutions Architect - SkyCargo",
                    "company": "Emirates",
                },
                "application_update": {
                    "status": "updated",
                    "reason": "application marked rejected",
                    "tracker_synced": True,
                },
                "date": "Fri, 17 Jul 2026 05:13:10 +0000",
            }
        ]
    )

    assert "Application rejected" in alert
    assert "Solutions Architect - SkyCargo" in alert
    assert "Status: rejected" in alert
    assert "Application tracker: synced" in alert


def test_gmail_watcher_formats_retry_alert_before_partial_outcome():
    alert = gmail_watcher.format_alert(
        [
            {
                "outcome": "rejected",
                "processing_error": "OperationalError",
            }
        ]
    )

    assert "processing failed" in alert
    assert "next watcher run can retry" in alert
    assert "Status update skipped" not in alert


class FakeModifyCall:
    def __init__(self, calls):
        self.calls = calls
    def execute(self):
        self.calls.append("execute")


class FakeMessages:
    def __init__(self):
        self.args = None
        self.calls = []
    def modify(self, **kwargs):
        self.args = kwargs
        return FakeModifyCall(self.calls)


class FakeUsers:
    def __init__(self):
        self.messages_obj = FakeMessages()
    def messages(self):
        return self.messages_obj


class FakeService:
    def __init__(self):
        self.users_obj = FakeUsers()
    def users(self):
        return self.users_obj


def test_gmail_watcher_marks_processed_message_read():
    service = FakeService()
    gmail_watcher.mark_message_read(service, "msg-1")
    messages = service.users_obj.messages_obj
    assert messages.args == {"userId": "me", "id": "msg-1", "body": {"removeLabelIds": ["UNREAD"]}}
    assert messages.calls == ["execute"]
