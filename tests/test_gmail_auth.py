import argparse
import json
import logging
import stat
from unittest.mock import Mock

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from jobhunter_integrations import gmail_auth as auth


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_GMAIL_ACCOUNT", "jobs@example.com")
    monkeypatch.setenv("JOBHUNTER_GOOGLE_ACCOUNT_CONFIG", str(tmp_path / "account.json"))
    service = Mock()
    service.users().getProfile().execute.return_value = {"emailAddress": "jobs@example.com"}
    monkeypatch.setattr(auth, "service_for_credentials", Mock(return_value=service))
    creds = Mock(valid=True, refresh_token="test-only-refresh")
    creds.has_scopes.return_value = True
    creds.to_json.return_value = '{"token": "test-only-token"}'
    monkeypatch.setattr(Credentials, "from_authorized_user_file", Mock(return_value=creds))
    return service, creds


def test_private_json_replaces_atomically_with_private_mode(tmp_path, monkeypatch):
    path = tmp_path / "private" / "token.json"
    auth.write_private_json(path, {"version": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    monkeypatch.setattr(auth.os, "replace", Mock(side_effect=OSError("test failure")))
    with pytest.raises(OSError):
        auth.write_private_json(path, {"version": 2})
    assert json.loads(path.read_text()) == {"version": 1}
    assert list(path.parent.iterdir()) == [path]


def test_account_config_is_required_and_recoverable(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUNTER_GMAIL_ACCOUNT", raising=False)
    path = tmp_path / "account.json"
    with pytest.raises(auth.GmailAuthError, match="Set JOBHUNTER"):
        auth.expected_account(config_path=path)
    auth.write_private_json(path, {"email": "Jobs@Example.com"})
    assert auth.expected_account(config_path=path) == "jobs@example.com"


def test_check_only_reads_profile(configured, tmp_path):
    service, creds = configured
    assert auth.gmail_service(tmp_path / "token.json") is service
    creds.refresh.assert_not_called()
    service.users().getProfile.assert_called_with(userId="me")
    service.users().messages.assert_not_called()


def test_refresh_is_persisted_only_after_mailbox_verification(configured, tmp_path):
    service, creds = configured
    path = tmp_path / "token.json"
    auth.gmail_service(path, force_refresh=True)
    creds.refresh.assert_called_once()
    assert json.loads(path.read_text()) == {"token": "test-only-token"}
    service.users().getProfile().execute.return_value = {"emailAddress": "personal@example.com"}
    path.write_text("unchanged")
    with pytest.raises(auth.GmailAuthError, match="does not match"):
        auth.gmail_service(path, force_refresh=True)
    assert path.read_text() == "unchanged"


@pytest.mark.parametrize("failure", ["scope", "offline", "revoked"])
def test_bad_credentials_fail_without_leaking_provider_details(configured, tmp_path, failure):
    service, creds = configured
    if failure == "scope":
        creds.has_scopes.return_value = False
    elif failure == "offline":
        creds.refresh_token = None
    else:
        creds.valid = False
        creds.refresh.side_effect = RuntimeError("private-provider-payload")
    with pytest.raises(auth.GmailAuthError) as error:
        auth.gmail_service(tmp_path / "token.json")
    assert "private-provider-payload" not in str(error.value)
    service.users().getProfile().execute.assert_not_called()


def test_authorize_requests_readonly_pkce_loopback_and_saves_verified_account(configured, tmp_path, monkeypatch):
    _, creds = configured
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "test-client", "client_secret": "test-secret", "token_uri": "https://untrusted.example"}}))
    flow = Mock()
    flow.run_local_server.return_value = creds
    factory = Mock(return_value=flow)
    monkeypatch.setattr(InstalledAppFlow, "from_client_config", factory)
    args = argparse.Namespace(account="jobs@example.com", client_secret=path, google_token=tmp_path / "token.json", port=8765, no_browser=True)
    auth.authorize(args)
    config, scopes = factory.call_args.args
    assert scopes == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert config["installed"]["token_uri"] == auth.TOKEN_URI
    assert factory.call_args.kwargs["autogenerate_code_verifier"] is True
    kwargs = flow.run_local_server.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["access_type"] == "offline"
    assert kwargs["open_browser"] is False
    assert json.loads((tmp_path / "account.json").read_text())["email"] == "jobs@example.com"


def test_authorize_does_not_replace_credentials_for_wrong_account(configured, tmp_path, monkeypatch):
    service, creds = configured
    service.users().getProfile().execute.return_value = {"emailAddress": "wrong@example.com"}
    path = tmp_path / "client.json"
    path.write_text('{"installed":{"client_id":"test","client_secret":"test"}}')
    token = tmp_path / "token.json"
    token.write_text("preserved")
    flow = Mock()
    flow.run_local_server.return_value = creds
    monkeypatch.setattr(InstalledAppFlow, "from_client_config", Mock(return_value=flow))
    logger = logging.getLogger("google_auth_oauthlib.flow")
    level = logger.level
    with pytest.raises(auth.GmailAuthError, match="does not match"):
        auth.authorize(argparse.Namespace(account="jobs@example.com", client_secret=path, google_token=token, port=8765, no_browser=True))
    assert token.read_text() == "preserved"
    assert logger.level == level
