"""Deterministic interleavings at confirmed configuration and account boundaries."""
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import Mock

from aiohttp.test_utils import TestClient, TestServer
import pytest

from jobhunter_integrations.web_oauth import VerifiedGoogleGrant
from jobhunter_service.scheduler import Scheduler
from jobhunter_service.service import JobHunterService
from jobhunter_service.state import private_json
from jobhunter_service.web import create_app


@pytest.fixture
def service(tmp_path):
    result = JobHunterService(tmp_path, 900, public_url="https://jobs.example.test", telegram_client=Mock())
    result.admin(900, "add", 123)
    result.authorize(123)
    apply_settings(result, {"accounts": {"gmail": {"account": "old@example.test", "enabled": True}},
                            "search": {"keywords": ["original-role"]}})
    return result


def apply_settings(service, patch):
    action = service.propose(123, patch)
    return service.confirm(123, action["action_id"])


def test_stale_member_cannot_roll_back_materialized_settings_or_archive_new_grant(service):
    stale = service._member(123)
    apply_settings(service, {"accounts": {"gmail": {"account": "new@example.test"}},
                             "search": {"keywords": ["new-role"]}})
    root = service.profile_dir(service._member(123))
    token = root / "secrets" / "gmail_token.json"
    private_json(token, {"fixture_account": "new@example.test"})
    # A scheduler can retain the old member while a Telegram confirmation wins.
    # Implementations may reject the stale object or reload the canonical one.
    try:
        service.materialize(stale)
    except (ValueError, PermissionError):
        pass
    current = json.loads(service._member(123)["settings"])
    assert json.loads((root / "config.json").read_text()) == current["search"]
    assert json.loads((root / "settings.json").read_text()) == current
    assert token.exists() and json.loads(token.read_text())["fixture_account"] == "new@example.test"


class Google:
    def authorization_url(self, kind, state, account):
        return {"url": "https://accounts.google.com/auth?" + urlencode({"state": state}),
                "code_verifier": "synthetic-verifier"}

    def exchange(self, kind, code, account, verifier):
        return VerifiedGoogleGrant(kind, account, {"fixture_account": account})


def test_account_change_cannot_be_followed_by_old_callback_grant_persistence(service, monkeypatch):
    entered = threading.Event()
    errors = []
    original = VerifiedGoogleGrant.persist

    def persist(grant, path):
        entered.set()
        # Give the independent Telegram worker a chance to confirm while the
        # callback is between its final member check and its filesystem write.
        # A correct per-profile lock delays that confirmation until this ends.
        time.sleep(0.15)
        return original(grant, path)

    def change_account():
        try:
            assert entered.wait(3)
            apply_settings(service, {"accounts": {"gmail": {"account": "new@example.test"}}})
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(VerifiedGoogleGrant, "persist", persist)
    changer = threading.Thread(target=change_account)
    changer.start()

    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=Google()))) as client:
            link = urlsplit(service.connect(123, "google", "gmail"))
            start = await client.get(link.path + "?" + link.query, allow_redirects=False)
            state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
            response = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert response.status in {200, 403}

    try:
        asyncio.run(scenario())
    finally:
        changer.join(timeout=3)
    assert not changer.is_alive()
    assert errors == []
    member = service._member(123)
    assert json.loads(member["settings"])["accounts"]["gmail"]["account"] == "new@example.test"
    token = service.profile_dir(member) / "secrets" / "gmail_token.json"
    assert not token.exists(), "The superseded account grant must not become the current token"


def test_mail_disabled_after_scheduling_snapshot_is_not_monitored(service, monkeypatch):
    root = service.profile_dir(service._member(123))
    private_json(root / "secrets" / "gmail_token.json", {"fixture_account": "old@example.test"})
    monitor = Mock()
    monkeypatch.setattr("jobhunter_integrations.gmail_monitor.run_candidate_monitor", monitor)
    scheduler = Scheduler(service, None, service.telegram_client)
    original = service._member
    changed = False

    def current_member(actor, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            apply_settings(service, {"accounts": {"gmail": {"enabled": False}}})
        return original(actor, **kwargs)

    monkeypatch.setattr(service, "_member", current_member)
    scheduler.monitor_due(datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc))
    assert json.loads(original(123)["settings"])["accounts"]["gmail"]["enabled"] is False
    monitor.assert_not_called()
