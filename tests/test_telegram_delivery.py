from unittest import mock

import pytest
import requests

import scraper


@pytest.mark.parametrize("status,body,expected", [
    (200, {"ok": True, "result": {"message_id": 123}}, True),
    (200, {"ok": False}, False),
    (200, {}, False),
    (200, {"ok": "true"}, False),
    (200, [], False),
    (429, {"ok": False}, False),
    (500, {"ok": True}, False),
])
def test_message_delivery_requires_successful_api_acknowledgement(status, body, expected):
    response = mock.Mock(status_code=status)
    response.json.return_value = body
    with mock.patch.object(scraper.requests, "post", return_value=response):
        assert scraper.send_telegram("test-token", "test-chat", "digest") is expected


def test_invalid_json_is_not_delivery_confirmation():
    response = mock.Mock(status_code=200)
    response.json.side_effect = ValueError("invalid JSON")
    with mock.patch.object(scraper.requests, "post", return_value=response):
        assert scraper.send_telegram("test-token", "test-chat", "digest") is False


def test_network_failure_does_not_expose_token_in_log(caplog):
    with mock.patch.object(scraper.requests, "post", side_effect=requests.Timeout(
        "https://api.telegram.org/botprivate-test-token/sendMessage timed out",
    )):
        assert scraper.send_telegram("private-test-token", "test-chat", "digest") is False
    assert "private-test-token" not in caplog.text
    assert "Timeout" in caplog.text


def test_individual_notifications_return_only_confirmed_ids():
    jobs = [{"id": "sent", "title": "Backend Lead", "company": "Test", "score": 80},
            {"id": "failed", "title": "Backend Engineer", "company": "Test", "score": 70}]
    with mock.patch.object(scraper, "send_telegram", side_effect=[True, True, False]), \
         mock.patch.object(scraper, "format_job_message", return_value="card"), \
         mock.patch.object(scraper, "job_inline_keyboard", return_value={}), \
         mock.patch.object(scraper.time, "sleep"):
        assert scraper.notify_new_jobs("tok", "chat", jobs) == ["sent"]


def test_empty_notifications_return_no_confirmed_ids():
    with mock.patch.object(scraper.requests, "post") as post:
        assert scraper.notify_new_jobs("tok", "chat", []) == []
    post.assert_not_called()
