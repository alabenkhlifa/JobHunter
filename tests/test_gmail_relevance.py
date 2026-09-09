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
    assert "<b>Application received</b>" in alert
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
    assert "&lt;a href=" in alert
    assert '<a href="https://invalid.example"' not in alert
