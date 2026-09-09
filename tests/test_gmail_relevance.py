import argparse
import base64
import datetime as dt
from unittest.mock import Mock

import pytest

from jobhunter_integrations import gmail_watcher as watcher


JOBS = [{"title": "CTO", "company": "ExampleCo", "stage": "submitted"}]


@pytest.mark.parametrize("text", [
    "Your account is protected with another factor.",
    "Update your CSS selector settings.",
    "A new delivery is scheduled for tomorrow.",
])
def test_role_and_ats_keywords_do_not_match_inside_other_words(text):
    assert watcher.is_relevant(text, JOBS) == (False, [])


@pytest.mark.parametrize("subject", [
    "2-Step Verification turned on",
    "Two-factor authentication enabled",
    "Security alert for your account",
    "Your password changed",
    "Reset your password",
    "New sign-in to your account",
    "Your verification code",
])
def test_account_events_are_not_application_alerts_even_with_job_keywords(subject):
    text = subject + " CTO ExampleCo application access settings"
    assert watcher.is_relevant(text, JOBS, subject=subject) == (False, [])


def test_google_account_sender_is_blocked_without_blocking_google_recruiters():
    text = "We have an update about your application."
    assert watcher.is_relevant(text, JOBS, sender="Google <NO-REPLY@accounts.google.com>") == (False, [])
    relevant, _ = watcher.is_relevant(text, JOBS, sender="Google Careers <careers@google.com>", subject="Your application")
    assert relevant


@pytest.mark.parametrize("text", [
    "Your CTO application at ExampleCo has progressed.",
    "Interview invitation for the Security Engineer position.",
    "We received your application through Lever.",
    "Updates on your applications and upcoming interviews.",
])
def test_genuine_application_replies_remain_relevant(text):
    assert watcher.is_relevant(text, JOBS)[0]


def test_company_names_do_not_match_inside_other_company_names():
    jobs = [{"title": "Backend Engineer", "company": "Arc", "stage": "submitted"}]
    text = "Your archive settings are up to date."
    assert watcher.is_relevant(text, jobs) == (False, [])
    assert watcher.match_active_application(text, jobs)[0] is None


def test_visible_text_ignores_styles_and_attributes_but_keeps_body_copy():
    raw = '<html><head><style>.cto { color: red; }</style></head><body class="application">Account notice<script>interview()</script></body></html>'
    payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(raw.encode()).decode()}}
    text = watcher.extract_visible_text(payload)
    assert text == "Account notice"
    assert watcher.is_relevant(text, JOBS) == (False, [])
    # Verification lookup still has access to the original HTML and links.
    assert watcher.extract_text(payload) == raw


def test_security_email_never_reaches_application_updates(tmp_path, monkeypatch):
    service = Mock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "security"}]}
    monkeypatch.setattr(watcher, "gmail_service", Mock(return_value=service))
    monkeypatch.setattr(watcher, "interested_jobs", Mock(return_value=JOBS))
    monkeypatch.setattr(watcher, "message_summary", Mock(return_value={
        "text": "2-Step Verification turned on. Another factor protects your application access.",
        "from": "no-reply@accounts.google.com", "subject": "2-Step Verification turned on",
    }))
    updater = Mock()
    monkeypatch.setattr(watcher, "process_application_outcome", updater)
    args = argparse.Namespace(google_token=tmp_path / "token.json", db_path=tmp_path / "jobs.db", state_path=tmp_path / "state.json", max_messages=25, query="newer_than:30d")
    matches, state = watcher.collect_mail(args)
    assert matches == []
    assert state["seen_message_ids"] == ["security"]
    updater.assert_not_called()
    service.users().messages().modify.assert_not_called()


def test_compact_alert_hides_matching_diagnostics_and_repeated_addresses():
    message = {
        "subject": "Application received",
        "from": "ExampleCo Careers <recruiter@example.com>",
        "date": "Wed, 09 Sep 2026 12:49:16 GMT",
        "snippet": "Application received candidate@example.com Thank you for applying.",
        "reasons": ["matches jobs/companies: cto"],
    }
    alert = watcher.format_alert([message])
    assert "<b>Review needed</b>" in alert
    assert "ExampleCo Careers" in alert
    assert "Thank you for applying." in alert
    for noise in ("Why:", "Snippet:", "Date:", "From:", "GMT", "cto", "candidate@example.com", "recruiter@example.com"):
        assert noise not in alert
    assert alert.count("Application received") == 1


def test_short_date_uses_selected_timezone_and_ignores_invalid_dates():
    date = "Wed, 09 Sep 2026 12:49:16 GMT"
    assert watcher.format_email_date(date, dt.timezone(dt.timedelta(hours=1))) == "9 Sep · 13:49"
    assert watcher.format_email_date("invalid") == ""


def test_alert_escapes_html_and_preserves_unmatched_outcome_warning():
    alert = watcher.format_alert([{
        "outcome": "interview_invited",
        "from": "ExampleCo Careers <recruiter@example.com>",
        "snippet": '<a href="https://invalid.example">unexpected HTML</a>',
        "application_update": {"status": "skipped", "reason": "internal match details"},
    }])
    assert "Couldn’t link this email to one application." in alert
    assert "ExampleCo Careers" in alert
    assert "internal match details" not in alert
    assert "unexpected HTML" not in alert
    assert '<a href="https://invalid.example"' not in alert


@pytest.mark.parametrize("message", [
    "We unfortunately decided that we will not continue the process with you.",
    "We won't be continuing the recruitment process with you.",
    "We decided not to continue the selection process with you.",
    "We cannot proceed with the application process with you.",
])
def test_rejection_when_employer_ends_process_with_candidate(message):
    assert watcher.classify_application_outcome(message)[0] == "rejected"


@pytest.mark.parametrize("message", [
    "We will continue the process with you.",
    "We will not continue the process until you upload your documents.",
    "We will not continue the process with other candidates.",
    "We received many applications and will review your profile.",
])
def test_process_mentions_do_not_imply_rejection(message):
    assert watcher.classify_application_outcome(message)[0] is None


@pytest.mark.parametrize("synced", [True, False])
def test_outcome_card_shows_tracking_result_instead_of_truncated_email(synced):
    message = {
        "outcome": "rejected",
        "matched_job": {"company": "ExampleCo", "title": "Technical Lead"},
        "application_update": {"status": "updated", "tracker_synced": synced},
        "snippet": "Dear Candidate, we received an overwhelming response " * 8,
    }
    alert = watcher.format_alert([message])
    assert "❌ <b>Application rejected</b>" in alert
    assert "<b>ExampleCo</b>\nTechnical Lead" in alert
    assert "Dear Candidate" not in alert
    assert ("Database and spreadsheet updated" in alert) == synced
    assert ("Spreadsheet update not confirmed" in alert) != synced


def test_unknown_reply_keeps_company_context_and_explicit_review_warning(tmp_path, monkeypatch):
    jobs = [{"id": "test-job", "company": "ExampleCo", "title": "Technical Lead", "stage": "submitted"}]
    message = {"text": "ExampleCo Technical Lead: we have an update to discuss.", "snippet": "<script>untrusted & text</script>"}
    writer = Mock()
    monkeypatch.setattr(watcher, "record_application_outcome", writer)
    watcher.process_application_outcome(message, jobs, tmp_path / "unused.db")
    writer.assert_not_called()
    alert = watcher.format_alert([message])
    assert "<b>Review needed</b>" in alert
    assert "<b>ExampleCo</b>" in alert
    assert "Status unchanged — review this email." in alert
    assert "&lt;script&gt;" in alert
    assert "<script>" not in alert


def test_receipt_is_linked_without_downgrading_application(tmp_path, monkeypatch):
    jobs = [{"id": "test-job", "company": "ExampleCo", "title": "Technical Lead", "stage": "interview_invited"}]
    message = {"text": "Thank you for your application to ExampleCo for Technical Lead. Your application will be reviewed by our team."}
    writer = Mock()
    monkeypatch.setattr(watcher, "record_application_outcome", writer)
    watcher.process_application_outcome(message, jobs, tmp_path / "unused.db")
    assert message["acknowledgement"]
    assert message["matched_job"]["id"] == "test-job"
    writer.assert_not_called()
    alert = watcher.format_alert([message])
    assert "📨 <b>Application received</b>" in alert
    assert "Receipt confirmation; no status change." in alert
    assert "updated" not in alert


def test_unclear_negative_reply_is_not_labeled_as_receipt(tmp_path):
    message = {"text": "We received your application. Unfortunately, circumstances have changed."}
    watcher.process_application_outcome(message, [], tmp_path / "unused.db")
    assert not message["acknowledgement"]
    assert "Review needed" in watcher.format_alert([message])


def test_generic_thanks_alone_does_not_hide_unknown_outcome(tmp_path):
    message = {"text": "Thank you for your application. Our team has an update for you."}
    watcher.process_application_outcome(message, [], tmp_path / "unused.db")
    assert not message["acknowledgement"]
    assert "Review needed" in watcher.format_alert([message])


def test_preview_truncates_at_word_boundary_with_ellipsis():
    preview = watcher.preview_text({"snippet": "longword " * 40})
    assert len(preview) <= 141
    assert preview.endswith("longword…")
