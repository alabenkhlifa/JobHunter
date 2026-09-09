import argparse
import base64
import datetime as dt
import json
from unittest.mock import Mock

import pytest

from jobhunter_integrations import gmail_verification, gmail_watcher


def watcher_fixture(tmp_path, monkeypatch, pages, *, seen=()):
    service = Mock()
    service.users().messages().list().execute.side_effect = pages
    monkeypatch.setattr(gmail_watcher, "gmail_service", Mock(return_value=service))
    monkeypatch.setattr(gmail_watcher, "interested_jobs", Mock(return_value=[]))
    monkeypatch.setattr(gmail_watcher, "message_summary", Mock(return_value={"text": "job application received"}))
    monkeypatch.setattr(gmail_watcher, "is_relevant", Mock(return_value=(True, ["application"])))
    monkeypatch.setattr(gmail_watcher, "process_application_outcome", Mock())
    args = argparse.Namespace(google_token=tmp_path / "token.json", db_path=tmp_path / "jobs.db", state_path=tmp_path / "state.json", query="newer_than:30d", max_messages=2)
    args.state_path.write_text(json.dumps({"seen_message_ids": list(seen)}))
    return service, args


def test_watcher_keeps_mail_read_flags_and_does_not_repeat(tmp_path, monkeypatch):
    page = {"messages": [{"id": "m1"}]}
    service, args = watcher_fixture(tmp_path, monkeypatch, [page, page])
    assert len(gmail_watcher.check_mail(args)) == 1
    assert gmail_watcher.check_mail(args) == []
    service.users().messages().modify.assert_not_called()
    assert args.state_path.stat().st_mode & 0o777 == 0o600


def test_watcher_paginates_past_seen_messages_without_evicting_ledger(tmp_path, monkeypatch):
    seen = [f"seen-{i}" for i in range(600)]
    service, args = watcher_fixture(tmp_path, monkeypatch, [
        {"messages": [{"id": "seen-1"}], "nextPageToken": "next"},
        {"messages": [{"id": "new"}]},
    ], seen=seen)
    assert len(gmail_watcher.check_mail(args)) == 1
    service.users().messages().list.assert_called_with(userId="me", q=args.query, maxResults=2, pageToken="next")
    assert len(json.loads(args.state_path.read_text())["seen_message_ids"]) == 601


def test_watcher_retries_processing_failure(tmp_path, monkeypatch):
    service, args = watcher_fixture(tmp_path, monkeypatch, [{"messages": [{"id": "retry"}]}])
    gmail_watcher.process_application_outcome.side_effect = RuntimeError("test failure")
    assert gmail_watcher.check_mail(args)[0]["processing_error"] == "RuntimeError"
    assert json.loads(args.state_path.read_text())["seen_message_ids"] == []


def test_watcher_does_not_alert_verification_codes(tmp_path, monkeypatch):
    service, args = watcher_fixture(tmp_path, monkeypatch, [{"messages": [{"id": "otp"}]}])
    gmail_watcher.message_summary.return_value = {"text": "Job application verification code: test-only"}
    assert gmail_watcher.check_mail(args) == []
    gmail_watcher.process_application_outcome.assert_not_called()
    assert json.loads(args.state_path.read_text())["seen_message_ids"] == ["otp"]


def test_corrupt_ledger_is_not_silently_replaced(tmp_path, monkeypatch):
    _, args = watcher_fixture(tmp_path, monkeypatch, [])
    args.state_path.write_text("broken")
    with pytest.raises(RuntimeError, match="state could not be read"):
        gmail_watcher.check_mail(args)
    assert args.state_path.read_text() == "broken"


def message(mid, timestamp, *, sender="noreply@ats.example.com", recipient="jobs@example.com"):
    return {"id": mid, "internalDate": str(int(timestamp.timestamp() * 1000)), "payload": {
        "mimeType": "text/plain", "headers": [{"name": "From", "value": sender}, {"name": "To", "value": recipient}],
        "body": {"data": base64.urlsafe_b64encode(b"test verification content").decode()},
    }}


def test_verification_checks_domain_recipient_time_and_newest_across_pages():
    now = dt.datetime.now(dt.timezone.utc)
    after = now - dt.timedelta(minutes=5)
    items = [
        message("valid-old", now - dt.timedelta(minutes=3)),
        message("lookalike", now, sender="noreply@evilats.example.com"),
        message("wrong-recipient", now, recipient="personal@example.com"),
        message("old", now - dt.timedelta(hours=1)),
        message("future", now + dt.timedelta(minutes=1)),
        message("newest", now - dt.timedelta(seconds=1)),
    ]
    service = Mock()
    service.users().messages().list().execute.side_effect = [
        {"messages": [{"id": m["id"]} for m in items[:3]], "nextPageToken": "next"},
        {"messages": [{"id": m["id"]} for m in items[3:]]},
    ]
    service.users().messages().get().execute.side_effect = items
    result = gmail_verification.find_message(service, account="jobs@example.com", sender_domain="ats.example.com", after=after, now=now)
    assert result["message_id"] == "newest"
    service.users().messages().modify.assert_not_called()


@pytest.mark.parametrize("minutes", [16, -1])
def test_verification_rejects_unbounded_or_future_window(minutes):
    now = dt.datetime.now(dt.timezone.utc)
    service = Mock()
    with pytest.raises(ValueError, match="15 minutes"):
        gmail_verification.find_message(service, account="jobs@example.com", sender_domain="ats.example.com", after=now - dt.timedelta(minutes=minutes), now=now)
    service.users.assert_not_called()
