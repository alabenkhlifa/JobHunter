import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobhunter_integrations import gmail_watcher, google_tracker


def make_application_db(tmp_path: Path, *, stage: str = "submitted") -> Path:
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
            stage,
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
            stage,
            "2026-07-10T15:21:41+00:00",
            "2026-07-10T15:21:41+00:00",
            "Submission confirmed.",
        ),
    )
    conn.commit()
    conn.close()
    return db


def test_tracker_formats_dates_and_status_colors():
    assert google_tracker.format_dt("2026-07-18T14:47:00+00:00").endswith("15:47") or google_tracker.format_dt("2026-07-18T14:47:00+00:00").endswith("14:47")
    assert google_tracker.sheet_text_dt("2026-07-18T15:47:00+00:00").startswith("'")
    assert google_tracker.status_color("submitted")["green"] > google_tracker.status_color("submitted")["red"]
    assert google_tracker.status_color("blocked_login_required")["red"] > google_tracker.status_color("blocked_login_required")["green"]
    assert google_tracker.status_color("rejected")["red"] > google_tracker.status_color("rejected")["green"]
    assert google_tracker.next_action("rejected", None) == "Application closed"
    assert google_tracker.next_action("interview_invited", None).startswith("Review and respond")
    assert google_tracker.next_action("assessment_requested", None) == "Review and complete assessment"
    assert google_tracker.next_action("action_required", None) == "Review requested action"
    assert google_tracker.next_action("application_progressed", None) == "Monitor email for next steps"
    assert "acceptance requires approval" in google_tracker.next_action("offer_received", None)


def test_tracker_assigns_non_white_color_to_every_status_family():
    statuses = [
        "new",
        "interested",
        "package_generated",
        "package_prepared",
        "draft_ready",
        "draft_inspected",
        "approved",
        "approved_to_prepare_apply",
        "resume_uploaded",
        "after_upload",
        "submitted",
        "submission_result",
        "application_progressed",
        "interview_invited",
        "assessment_requested",
        "action_required",
        "offer_received",
        "rejected",
        "blocked_login_required",
        "blocked_privacy_notice",
        "blocked_unknown_questions",
        "blocked_site_challenge",
        "blocked_resume_upload_approval",
        "blocked_submit_approval",
        "failed",
        "unavailable",
        "skipped",
        "withdrawn",
        "closed",
        "archived",
        "future_status_not_yet_mapped",
        "",
    ]

    for status in statuses:
        color = google_tracker.status_color(status)
        assert set(color) == {"red", "green", "blue"}
        assert color != {"red": 1.0, "green": 1.0, "blue": 1.0}

    assert google_tracker.status_color("rejected") == google_tracker.STATUS_PALETTE["red"]
    assert google_tracker.status_color(" Rejected ") == google_tracker.STATUS_PALETTE["red"]
    assert google_tracker.status_color("blocked_privacy_notice") == google_tracker.STATUS_PALETTE["blocked"]
    assert google_tracker.status_color("future_status_not_yet_mapped") == google_tracker.STATUS_PALETTE["neutral"]


def test_tracker_applies_status_background_to_entire_data_row():
    statuses = ["submitted", "rejected", "interview_invited", "unknown_future_status"]
    values = [google_tracker.HEADERS]
    for status in statuses:
        row = [""] * len(google_tracker.HEADERS)
        row[2] = status
        values.append(row)

    requests = google_tracker.formatting_requests(123, values)
    backgrounds = [
        request["repeatCell"]
        for request in requests
        if request.get("repeatCell", {}).get("fields") == "userEnteredFormat.backgroundColor"
    ]

    assert len(backgrounds) == len(statuses)
    for index, (status, repeat) in enumerate(zip(statuses, backgrounds), start=1):
        assert repeat["range"] == {
            "sheetId": 123,
            "startRowIndex": index,
            "endRowIndex": index + 1,
            "startColumnIndex": 0,
            "endColumnIndex": len(google_tracker.HEADERS),
        }
        assert repeat["cell"]["userEnteredFormat"]["backgroundColor"] == google_tracker.status_color(status)


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


def test_gmail_watcher_classifies_positive_outcomes_without_promoting_acknowledgements():
    messages = [
        ("interview_invited", "We would like to schedule an interview for this role."),
        ("interview_invited", "Your application has been accepted for an interview."),
        ("assessment_requested", "You are invited to complete an online assessment for this role."),
        ("action_required", "Action required: please provide additional information for your application."),
        ("application_progressed", "We'd like to proceed with your application."),
        ("application_progressed", "You are invited to the next stage of the recruitment process."),
        ("offer_received", "We are pleased to extend you a job offer."),
    ]

    for expected, message in messages:
        outcome, reasons = gmail_watcher.classify_application_outcome(message)
        assert outcome == expected
        assert reasons

    acknowledgements = [
        "We received your application.",
        "We will contact you if we decide to move forward with your application.",
        "Your application is currently under review.",
        "Read our guide to negotiating a job offer.",
    ]
    for message in acknowledgements:
        assert gmail_watcher.classify_application_outcome(message) == (None, [])


def test_gmail_watcher_matches_one_active_application_by_exact_title():
    jobs = [
        {
            "id": "job-skycargo",
            "title": "Solutions Architect - SkyCargo",
            "company": "Emirates",
            "stage": "application_progressed",
        },
        {
            "id": "job-network",
            "title": "Solutions Architect - Network Operations",
            "company": "Emirates",
            "stage": "submitted",
        },
    ]

    job, reason = gmail_watcher.match_active_application(
        "Update for Solutions Architect - SkyCargo at Emirates", jobs
    )

    assert job["id"] == "job-skycargo"
    assert reason == "exact job title"


def test_gmail_watcher_matches_only_active_application_for_company():
    jobs = [
        {
            "id": "job-skycargo",
            "title": "Solutions Architect - SkyCargo",
            "company": "Emirates",
            "stage": "submitted",
        },
        {
            "id": "job-other",
            "title": "Backend Engineer",
            "company": "ExampleCo",
            "stage": "submitted",
        },
    ]

    job, reason = gmail_watcher.match_active_application(
        "We would like to proceed with your application at Emirates.",
        jobs,
    )

    assert job["id"] == "job-skycargo"
    assert reason == "only active application for matched company"


def test_gmail_watcher_loads_post_submission_stages_as_active(tmp_path: Path):
    db = make_application_db(tmp_path, stage="interview_invited")

    jobs = gmail_watcher.interested_jobs(db)

    assert len(jobs) == 1
    assert jobs[0]["id"] == "job-skycargo"
    assert jobs[0]["stage"] == "interview_invited"


def test_gmail_watcher_does_not_match_ambiguous_or_pre_submission_application():
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
    job, reason = gmail_watcher.match_active_application(
        "Backend Engineer application at ExampleCo", ambiguous
    )
    assert job is None
    assert "multiple" in reason

    job, reason = gmail_watcher.match_active_application("Application update from ExampleCo", ambiguous)
    assert job is None
    assert reason == "multiple active applications matched the same company"

    job, reason = gmail_watcher.match_active_application(
        "Backend Engineer application at ExampleCo",
        [{**ambiguous[0], "stage": "package_generated"}],
    )
    assert job is None
    assert "no unique active" in reason


def test_gmail_watcher_records_rejection_and_syncs_tracker_once(tmp_path: Path, monkeypatch):
    db = make_application_db(tmp_path)
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
    assert repeated["status"] == "already_recorded"
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


def test_gmail_watcher_records_positive_progression_without_downgrading_offer(
    tmp_path: Path,
    monkeypatch,
):
    db = make_application_db(tmp_path)
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
    now = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)

    interview = gmail_watcher.record_application_outcome(
        db,
        job,
        "interview_invited",
        now=now,
    )
    repeated = gmail_watcher.record_application_outcome(
        db,
        job,
        "interview_invited",
        now=now,
    )
    offer = gmail_watcher.record_application_outcome(
        db,
        job,
        "offer_received",
        now=now,
    )
    downgrade = gmail_watcher.record_application_outcome(
        db,
        job,
        "application_progressed",
        now=now,
    )

    assert interview["status"] == "updated"
    assert repeated["status"] == "already_recorded"
    assert offer["status"] == "updated"
    assert downgrade["status"] == "skipped"
    assert "offer_received" in downgrade["reason"]
    assert sync_calls == ["sync", "sync"]

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

    assert application[0] == "offer_received"
    assert application[1] == "2026-07-10T15:21:41+00:00"
    assert "classified as interview_invited" in application[2]
    assert "classified as offer_received" in application[2]
    assert job_status == "offer_received"


def test_gmail_watcher_can_record_rejection_after_interview(tmp_path: Path, monkeypatch):
    db = make_application_db(tmp_path, stage="interview_invited")
    monkeypatch.setattr("scraper.sync_application_tracker_if_enabled", lambda: True)
    job = {
        "id": "job-skycargo",
        "title": "Solutions Architect - SkyCargo",
        "company": "Emirates",
        "stage": "interview_invited",
    }

    result = gmail_watcher.record_application_outcome(db, job, "rejected")

    assert result["status"] == "updated"
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT stage FROM applications WHERE job_id = ?",
        ("job-skycargo",),
    ).fetchone()[0] == "rejected"
    conn.close()


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


def test_gmail_watcher_formats_explicit_positive_outcome_alerts():
    expected_labels = {
        "interview_invited": "Interview invitation received",
        "assessment_requested": "Assessment requested",
        "action_required": "Application action required",
        "application_progressed": "Application progressed",
        "offer_received": "Job offer received",
    }

    for outcome, label in expected_labels.items():
        alert = gmail_watcher.format_alert(
            [
                {
                    "outcome": outcome,
                    "matched_job": {
                        "id": "job-skycargo",
                        "title": "Solutions Architect - SkyCargo",
                        "company": "Emirates",
                    },
                    "application_update": {
                        "status": "updated",
                        "reason": f"application marked {outcome}",
                        "tracker_synced": True,
                    },
                    "date": "Sat, 18 Jul 2026 05:13:10 +0000",
                }
            ]
        )

        assert label in alert
        assert f"Status: {outcome}" in alert
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
