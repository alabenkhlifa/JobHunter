import argparse
import json
from unittest.mock import Mock

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from jobhunter_integrations import google_tracker_auth as auth
from jobhunter_integrations.google_tracker import HEADERS, SCOPES


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_GMAIL_ACCOUNT", "jobs@example.com")
    monkeypatch.setenv("JOBHUNTER_GOOGLE_ACCOUNT_CONFIG", str(tmp_path / "account.json"))
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "gmail.json"))
    sheets, drive = Mock(), Mock()
    drive.about().get().execute.return_value = {"user": {"emailAddress": "jobs@example.com"}}
    monkeypatch.setattr(auth, "services_for_credentials", Mock(return_value=(sheets, drive)))
    credentials = Mock(valid=True, refresh_token="test-only-refresh")
    credentials.has_scopes.return_value = True
    credentials.to_json.return_value = '{"token":"test-only-token"}'
    monkeypatch.setattr(Credentials, "from_authorized_user_file", Mock(return_value=credentials))
    return sheets, drive, credentials


def test_tracker_consent_uses_only_sheets_drive_and_preserves_mailbox(configured, tmp_path, monkeypatch):
    _, _, credentials = configured
    client = tmp_path / "client.json"
    client.write_text('{"installed":{"client_id":"test","client_secret":"test"}}')
    mailbox = tmp_path / "gmail.json"
    mailbox.write_text("preserved")
    token = tmp_path / "tracker.json"
    flow = Mock()
    flow.run_local_server.return_value = credentials
    factory = Mock(return_value=flow)
    monkeypatch.setattr(InstalledAppFlow, "from_client_config", factory)
    auth.authorize(argparse.Namespace(account=None, client_secret=client, google_token=token, port=8765, no_browser=True))
    assert factory.call_args.args[1] == SCOPES
    assert factory.call_args.kwargs["autogenerate_code_verifier"] is True
    assert flow.run_local_server.call_args.kwargs["host"] == "127.0.0.1"
    assert mailbox.read_text() == "preserved"
    assert json.loads(token.read_text()) == {"token": "test-only-token"}
    assert token.stat().st_mode & 0o777 == 0o600


def test_tracker_refuses_mailbox_token_path(configured, tmp_path):
    with pytest.raises(auth.GmailAuthError, match="separate tracker token"):
        auth.checked_services(tmp_path / "gmail.json")
    with pytest.raises(auth.GmailAuthError, match="separate tracker token"):
        auth.authorize(argparse.Namespace(google_token=tmp_path / "gmail.json"))
    Credentials.from_authorized_user_file.assert_not_called()


def test_check_reads_only_drive_identity(configured, tmp_path):
    sheets, drive, credentials = configured
    assert auth.checked_services(tmp_path / "tracker.json") == (sheets, drive)
    drive.about().get.assert_called_with(fields="user(emailAddress)")
    drive.files.assert_not_called()
    sheets.spreadsheets.assert_not_called()
    credentials.refresh.assert_not_called()


@pytest.mark.parametrize("failure", ["scope", "offline", "refresh"])
def test_tracker_rejects_bad_grants_without_leaking_details(configured, tmp_path, failure):
    _, drive, credentials = configured
    if failure == "scope":
        credentials.has_scopes.return_value = False
    elif failure == "offline":
        credentials.refresh_token = None
    else:
        credentials.valid = False
        credentials.refresh.side_effect = RuntimeError("private-provider-payload")
    with pytest.raises(auth.GmailAuthError) as error:
        auth.checked_services(tmp_path / "tracker.json")
    assert "private-provider-payload" not in str(error.value)
    drive.about().get().execute.assert_not_called()


def test_refresh_saved_only_after_account_check(configured, tmp_path):
    _, drive, credentials = configured
    token = tmp_path / "tracker.json"
    auth.checked_services(token, force_refresh=True)
    credentials.refresh.assert_called_once()
    assert json.loads(token.read_text()) == {"token": "test-only-token"}
    token.write_text("preserved")
    drive.about().get().execute.return_value = {"user": {"emailAddress": "wrong@example.com"}}
    with pytest.raises(auth.GmailAuthError, match="does not match"):
        auth.checked_services(token, force_refresh=True)
    assert token.read_text() == "preserved"


def test_inspect_resolves_gid_quotes_title_and_never_writes():
    sheets = Mock()
    sheets.spreadsheets().get().execute.return_value = {
        "properties": {"title": "Application tracker"},
        "sheets": [{"properties": {"sheetId": 123, "title": "Ala's Applications"}}],
    }
    sheets.spreadsheets().values().get().execute.return_value = {"values": [HEADERS]}
    result = auth.inspect_tracker(sheets, "test-sheet", 123)
    assert result["read_access"] is True
    assert result["tabs"][0]["expected_headers"] is True
    sheets.spreadsheets().values().get.assert_called_with(
        spreadsheetId="test-sheet", range="'Ala''s Applications'!A1:N1", valueRenderOption="FORMULA",
    )
    sheets.spreadsheets().batchUpdate.assert_not_called()
    sheets.spreadsheets().values().update.assert_not_called()
    sheets.spreadsheets().values().clear.assert_not_called()


def test_missing_gid_does_not_create_tab():
    sheets = Mock()
    sheets.spreadsheets().get().execute.return_value = {"sheets": []}
    with pytest.raises(auth.GmailAuthError, match="not found"):
        auth.inspect_tracker(sheets, "test-sheet", 123)
    sheets.spreadsheets().batchUpdate.assert_not_called()
    sheets.spreadsheets().values.assert_not_called()


def test_tracker_default_token_does_not_fall_back_to_mailbox(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_TRACKER_GOOGLE_TOKEN_PATH", raising=False)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "/tmp/gmail.json")
    assert auth.default_token_path().name == "google_tracker_token.json"
