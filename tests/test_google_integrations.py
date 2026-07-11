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


def test_gmail_watcher_ignores_google_sheet_share_mail_even_with_application_word():
    jobs = [{"title": "Backend Engineer", "company": "ExampleCo"}]
    text = (
        "Ala Khlifa via Google Sheets drive-shares-dm-noreply@google.com "
        "Spreadsheet shared with you: Job Applications has invited you to edit "
        "the following spreadsheet: Job Applications"
    )
    relevant, reasons = gmail_watcher.is_relevant(text, jobs)
    assert not relevant
    assert reasons == []


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
