"""Synthetic recruiter mail only: all providers and delivery are local fakes."""
import argparse
import base64
import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest

from jobhunter_integrations import gmail_monitor, gmail_watcher, google_tracker, google_tracker_sync
from test_google_integrations import make_application_db


@pytest.mark.parametrize("body,outcome,label", [
    (
        "Following our discussion, we would like to invite you to a second interview. "
        "Please let us know your availability.",
        "interview_invited", "Interview invitation received",
    ),
    (
        "Your interview is confirmed for Thursday at 10:00.",
        "interview_invited", "Interview invitation received",
    ),
    (
        "We have reviewed your application, and unfortunately, we are unable to offer "
        "you a position at this time.",
        "rejected", "Application rejected",
    ),
    (
        "Thank you for your interest in Denodo. After careful consideration, we have "
        "decided to move forward with other candidates whose qualifications more closely "
        "match our requirements.",
        "rejected", "Application rejected",
    ),
    (
        "Thank you for applying to Denodo. Unfortunately, we will not be moving forward "
        "with your candidacy at this time.",
        "rejected", "Application rejected",
    ),
    (
        "Thank you for applying to Denodo. Please let us know your availability "
        "for a technical interview next week.",
        "interview_invited", "Interview invitation received",
    ),
    (
        "Following up on your Denodo application: could you please send us your "
        "updated CV and confirm your availability?",
        "action_required", "Application action required",
    ),
])
def test_full_content_to_verified_sheet_and_outcome_alert(tmp_path, monkeypatch, body, outcome, label):
    db = make_application_db(tmp_path)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE jobs SET company='Denodo', title='Senior Solutions Engineer'")
    before = [google_tracker.HEADERS, *google_tracker.rows_from_db(db, tmp_path)]
    sheets, drive, gmail = Mock(), Mock(), Mock()
    sheets.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {
        "sheetId": 123, "title": "Applications", "gridProperties": {"rowCount": 100},
    }}]}
    # Read-before-write, concurrent-edit guard, then exact read-back of the write.
    def read_sheet():
        if sheets.spreadsheets().batchUpdate.called:
            incoming = google_tracker.rows_from_db(db, tmp_path)
            merged, _ = google_tracker_sync.merge_rows(before, incoming, repo_root=tmp_path)
            return {"values": google_tracker_sync.newest_first_requests(123, merged)[1]}
        return {"values": before}
    sheets.spreadsheets().batchUpdate.reset_mock()
    sheets.spreadsheets().values().get().execute.side_effect = read_sheet
    monkeypatch.setattr(google_tracker, "google_services", lambda _: (sheets, drive))
    tracker_args = argparse.Namespace(
        google_token=tmp_path / "fake-tracker-token.json", spreadsheet_id="fake-sheet", sheet_id=123,
        tab_name="Applications", db_path=db, repo_root=tmp_path, drive_state=tmp_path / "tracker/files.json",
        drive_folder_name="Evidence", dry_run=False,
    )
    sync = Mock(side_effect=lambda: bool(google_tracker_sync.sync_tracker(tracker_args)["verified_rows"]))
    monkeypatch.setattr(gmail_watcher, "gmail_service", lambda _: gmail)
    gmail.users().messages().list().execute.return_value = {"messages": [{"id": "synthetic-mail"}]}
    # The real decision is beyond a plausible Gmail preview, inside nested HTML MIME.
    copy = "Thank you for your application. " + "We appreciate your interest. " * 30 + body
    gmail.users().messages().get().execute.return_value = {
        "snippet": "Thank you for your application. We appreciate your interest.",
        "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": "Subject", "value": "Your application at Denodo"},
            {"name": "From", "value": "Denodo Careers <careers@example.test>"},
        ], "parts": [{"mimeType": "multipart/alternative", "parts": [{
            "mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(
                f"<html><style>.rejected {{color: red}}</style><p>{copy}</p></html>".encode()
            ).decode()},
        }]}]},
    }
    gmail.users().messages().get.reset_mock()
    args = argparse.Namespace(google_token=tmp_path / "fake-token.json", db_path=db,
                              state_path=tmp_path / "seen.json", max_messages=25,
                              query="newer_than:30d", tracker_sync=sync)
    send = Mock(return_value=True)
    gmail_monitor.run_monitor(args, send)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT stage, submitted_at FROM applications").fetchone() == (
            outcome, "2026-07-10T15:21:41+00:00",
        )
        assert conn.execute("SELECT status FROM jobs").fetchone()[0] == outcome
    sync.assert_called_once_with()
    sheets.spreadsheets().batchUpdate.assert_called_once()
    assert sheets.spreadsheets().values().get().execute.call_count == 3
    alert = send.call_args.args[0]
    assert label in alert
    assert "Database and spreadsheet updated" in alert
    assert "Status unchanged" not in alert
    assert json.loads(args.state_path.read_text())["seen_message_ids"] == ["synthetic-mail"]
    gmail.users().messages().get.assert_called_once_with(userId="me", id="synthetic-mail", format="full")
    gmail.users().messages().modify.assert_not_called()
    send.reset_mock()
    gmail_monitor.run_monitor(args, send)
    send.assert_not_called()
    sync.assert_called_once()


@pytest.mark.parametrize("text", [
    "We received your application. We regret to inform you that our reply may be delayed.",
    "We received your application. If we are unable to offer you a position, we will notify you.",
    "We received your application. We cannot offer you a position until interviews are complete.",
    "We received your application. If selected, please share your availability for an interview.",
    "We received your application. Other candidates will be interviewed before a decision is made.",
    "We will contact you about next steps once we have reviewed your application.",
    "Unfortunately, our recruiter is away this week; your application remains under review.",
])
def test_generic_conditional_or_acknowledgement_is_not_an_outcome(text):
    assert gmail_watcher.classify_application_outcome(text) == (None, [])


@pytest.mark.parametrize("mime,body", [
    ("text/html", '<p>Denodo would like to schedule a technical interview.</p>'
     '<div class="gmail_quote"><blockquote>We are unable to offer you a position at this time.</blockquote></div>'),
    ("text/plain", "Denodo would like to schedule a technical interview.\n\nOn Monday, Recruiter wrote:\n"
     "> We are unable to offer you a position at this time."),
])
def test_current_interview_is_not_rejected_by_quoted_old_mail(tmp_path, mime, body):
    db = make_application_db(tmp_path)
    payload = {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}
    visible = gmail_watcher.extract_visible_text(payload)
    summary = {"subject": "Re: Your application", "body_text": visible, "text": visible}
    jobs = [{"id": "job-skycargo", "company": "Denodo", "title": "Senior Solutions Engineer", "stage": "submitted"}]
    gmail_watcher.process_application_outcome(summary, jobs, db, tracker_sync=lambda: False)
    assert summary["outcome"] == "interview_invited"
    assert "Interview invitation received" in gmail_watcher.format_alert([summary])


@pytest.mark.parametrize("text", [
    "Your Backend Engineer application at OtherCo. We cannot proceed.",
    "Your Backend Engineer application at OtherCo, not Denodo.",
])
def test_shared_title_never_overrides_explicit_other_employer(text):
    jobs = [
        {"id": "denodo", "company": "Denodo", "title": "Backend Engineer", "stage": "submitted"},
        {"id": "other", "company": "OtherCo", "title": "Platform Developer", "stage": "submitted"},
    ]
    job, reason = gmail_watcher.match_active_application(text, jobs)
    assert job is None
    assert reason


def test_fuzzy_title_never_overrides_explicit_other_employer():
    jobs = [
        {"id": "denodo", "company": "Denodo", "title": "Senior Cloud Infrastructure Engineer", "stage": "submitted"},
        {"id": "other", "company": "OtherCo", "title": "Platform Developer", "stage": "submitted"},
    ]
    job, reason = gmail_watcher.match_active_application(
        "OtherCo: Regarding your Cloud Infrastructure application, we are unable to offer you a position.", jobs,
    )
    # The unique explicit employer may match; a conflicting fuzzy title must not.
    assert job is not None
    assert job["id"] == "other"
    assert reason == "only active application for matched company"


def test_same_title_at_two_employers_uses_company_to_select_exact_application():
    jobs = [
        {"id": "denodo", "company": "Denodo", "title": "Backend Engineer", "stage": "submitted"},
        {"id": "other", "company": "OtherCo", "title": "Backend Engineer", "stage": "submitted"},
    ]
    job, _ = gmail_watcher.match_active_application("Your Backend Engineer application at Denodo", jobs)
    assert job is not None
    assert job["id"] == "denodo"


def test_html_alternative_is_not_combined_with_stale_plaintext_or_attachments():
    def part(mime, text, **extra):
        return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}, **extra}
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            part("text/plain", "We are unable to offer you a position at this time."),
            part("text/html", "<p>We would like to invite you to a second interview.</p>"),
        ]},
        part("text/plain", "We are unable to offer you a position at this time.", filename="old-message.txt"),
    ]}
    outcome, _ = gmail_watcher.classify_application_outcome(gmail_watcher.extract_visible_text(payload))
    assert outcome == "interview_invited"


def test_html_inline_conditional_is_not_mistaken_for_an_actual_rejection():
    body = "<p>If we are <b>unable to offer you a position</b>, we will notify you.</p>"
    payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}
    assert gmail_watcher.classify_application_outcome(gmail_watcher.extract_visible_text(payload)) == (None, [])


@pytest.mark.parametrize("body,outcome", [
    ("We are unable to offer you a position, but please contact us if you have questions.", "rejected"),
    ("Your interview is confirmed for Thursday, please let us know if you need to reschedule.", "interview_invited"),
])
def test_actual_outcome_survives_unrelated_conditional_contact_clause(body, outcome):
    assert gmail_watcher.classify_application_outcome(body)[0] == outcome
    assert gmail_watcher.outcome_detail(body, outcome)


@pytest.mark.parametrize("mime,body", [
    ("text/plain", "If selected,\nplease share your availability for an interview."),
    ("text/plain", "If selected,\nplease let us know your availability for an interview."),
    ("text/html", "<p>If selected, <span>please share your availability for an interview.</span></p>"),
])
def test_wrapped_conditional_interview_does_not_advance_application(tmp_path, mime, body):
    payload = {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}
    visible = gmail_watcher.extract_visible_text(payload)
    assert gmail_watcher.classify_application_outcome(visible) == (None, [])
    db = make_application_db(tmp_path)
    summary = {"subject": "Your application", "body_text": visible, "text": "Denodo " + visible}
    jobs = [{"id": "job-skycargo", "company": "Denodo", "title": "Senior Solutions Engineer", "stage": "submitted"}]
    sync = Mock()
    gmail_watcher.process_application_outcome(summary, jobs, db, tracker_sync=sync)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT stage FROM applications").fetchone()[0] == "submitted"
    sync.assert_not_called()


@pytest.mark.parametrize("body,expected", [
    ("We are not saying that we are unable to offer you a position.", None),
    ("We are not saying that we are unable to offer you a position. Your interview is confirmed for Thursday.", "interview_invited"),
])
def test_negated_rejection_is_not_a_decision(body, expected):
    assert gmail_watcher.classify_application_outcome(body)[0] == expected


def test_outcome_detail_handles_line_wrapped_email_copy():
    body = "We are unable to offer you\na position at this time."
    assert "unable to offer you a position" in gmail_watcher.outcome_detail(body, "rejected")


def test_matching_ignores_stale_snippet_company_when_full_body_is_available():
    service = Mock()
    body = "Your Backend Engineer application at Denodo. We are unable to offer you a position."
    service.users().messages().get().execute.return_value = {
        "snippet": "OtherCo application update", "payload": {"mimeType": "text/plain", "body": {
            "data": base64.urlsafe_b64encode(body.encode()).decode()}, "headers": [],
        },
    }
    summary = gmail_watcher.message_summary(service, "synthetic")
    jobs = [
        {"id": "denodo", "company": "Denodo", "title": "Backend Engineer", "stage": "submitted"},
        {"id": "other", "company": "OtherCo", "title": "Backend Engineer", "stage": "submitted"},
    ]
    job, _ = gmail_watcher.match_active_application(summary["text"], jobs)
    assert job is not None
    assert job["id"] == "denodo"


def test_sheet_readback_failure_preserves_local_outcome_and_does_not_claim_sync(tmp_path):
    db = make_application_db(tmp_path)
    summary = {"subject": "Your application", "body_text": "We are unable to offer you a position at this time.",
               "text": "Denodo We are unable to offer you a position at this time."}
    jobs = [{"id": "job-skycargo", "company": "Denodo", "title": "Senior Solutions Engineer", "stage": "submitted"}]
    sync = Mock(side_effect=RuntimeError("Application tracker read-back did not match"))
    gmail_watcher.process_application_outcome(summary, jobs, db, tracker_sync=sync)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT stage FROM applications").fetchone()[0] == "rejected"
    alert = gmail_watcher.format_alert([summary])
    assert "Application rejected" in alert
    assert "Outcome recorded in database" in alert
    assert "Spreadsheet update not confirmed" in alert
    assert "Database and spreadsheet updated" not in alert


def test_outcome_alert_quotes_current_body_not_stale_preview(tmp_path):
    db = make_application_db(tmp_path)
    body = (
        "We would like to invite you to a second interview on Thursday at 10:00. "
        "Please contact recruiter@example.test or https://example.test/private?token=secret."
    )
    summary = {"subject": "Application update", "body_text": body,
               "snippet": "We received your application", "text": "Denodo " + body}
    jobs = [{"id": "job-skycargo", "company": "Denodo", "title": "Senior Solutions Engineer", "stage": "submitted"}]
    gmail_watcher.process_application_outcome(summary, jobs, db, tracker_sync=lambda: False)
    alert = gmail_monitor.notification_batches([summary])[0]
    assert "second interview on Thursday at 10:00" in alert
    assert "We received your application" not in alert
    assert "@" not in alert
    assert "https://" not in alert


def test_outcome_detail_redacts_sensitive_copy_and_escapes_markup(tmp_path):
    db = make_application_db(tmp_path)
    body = ("We would like to invite you to a second interview <Thursday> at 10:00, "
            "email recruiter@example.test, https://example.test/private, phone +44 7700 900123, "
            "password=hunter2, code: 123456.")
    summary = {"subject": "Application update", "body_text": body, "text": "Denodo " + body}
    jobs = [{"id": "job-skycargo", "company": "Denodo", "title": "Senior Solutions Engineer", "stage": "submitted"}]
    gmail_watcher.process_application_outcome(summary, jobs, db, tracker_sync=lambda: False)
    alert = gmail_monitor.notification_batches([summary])[0]
    assert "&lt;Thursday&gt;" in alert
    for secret in ("recruiter@example.test", "https://example.test", "900123", "hunter2", "123456", "<Thursday>"):
        assert secret not in alert
