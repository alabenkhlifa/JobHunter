import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from jobhunter_integrations import web_oauth as oauth


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"web": {
        "client_id": "test-client", "client_secret": "test-client-secret",
        "redirect_uris": ["https://jobs.example.com/google/callback"],
        "auth_uri": "https://untrusted.example/auth", "token_uri": "https://untrusted.example/token",
    }}))
    return oauth.GoogleOAuthClient(path, "https://jobs.example.com/google/callback")


@pytest.mark.parametrize("kind", ["gmail", "tracker"])
def test_consent_keeps_account_scopes_and_pkce_separate(client, kind):
    result = client.authorization_url(kind, "s" * 64, "Candidate@Example.com")
    url = urlsplit(result["url"])
    values = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert values["scope"][0].split() == list(oauth.SCOPES[kind])
    assert values["state"] == ["s" * 64]
    assert values["login_hint"] == ["candidate@example.com"]
    assert values["include_granted_scopes"] == ["false"]
    assert values["access_type"] == ["offline"]
    assert values["code_challenge_method"] == ["S256"]
    assert result["code_verifier"] not in result["url"]


def mock_exchange(monkeypatch, kind="gmail", *, account="candidate@example.com", granted=None):
    credentials = Credentials("private-token", refresh_token="private-refresh",
                              token_uri=oauth.gmail_auth.TOKEN_URI, client_id="test", client_secret="private-client",
                              scopes=oauth.SCOPES[kind], granted_scopes=granted)
    flow = Mock(credentials=credentials)
    factory = Mock(return_value=flow)
    monkeypatch.setattr(Flow, "from_client_config", factory)
    gmail, sheets, drive = Mock(), Mock(), Mock()
    gmail.users().getProfile().execute.return_value = {"emailAddress": account}
    drive.about().get().execute.return_value = {"user": {"emailAddress": account}}
    monkeypatch.setattr(oauth.gmail_auth, "service_for_credentials", Mock(return_value=gmail))
    monkeypatch.setattr(oauth.google_tracker_auth, "services_for_credentials", Mock(return_value=(sheets, drive)))
    gmail.reset_mock()
    drive.reset_mock()
    return flow, factory, gmail, drive


@pytest.mark.parametrize("kind", ["gmail", "tracker"])
def test_only_verified_grant_is_returned_and_storage_is_explicit(client, monkeypatch, tmp_path, kind):
    flow, factory, gmail, drive = mock_exchange(monkeypatch, kind)
    monkeypatch.setenv("JOBHUNTER_GMAIL_ACCOUNT", "owner@example.com")
    result = client.exchange(kind, "private-code", "candidate@example.com", "v" * 64)
    assert result.kind == kind and result.account == "candidate@example.com"
    assert result.token["token"] == "private-token"
    assert "private" not in repr(result)
    assert list(tmp_path.iterdir()) == [client.client_config_path]
    flow.fetch_token.assert_called_once_with(code="private-code", timeout=30)
    assert factory.call_args.kwargs["code_verifier"] == "v" * 64
    assert factory.call_args.args[0]["web"]["token_uri"] == oauth.gmail_auth.TOKEN_URI
    if kind == "gmail":
        drive.about.assert_not_called()
    else:
        gmail.users.assert_not_called()
    path = tmp_path / kind / "token.json"
    result.persist(path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["gmail", "tracker"])
def test_wrong_account_fails_without_returning_or_persisting_token(client, monkeypatch, tmp_path, kind):
    mock_exchange(monkeypatch, kind, account="owner@example.com")
    with pytest.raises(oauth.GoogleOAuthError, match="does not match"):
        client.exchange(kind, "private-code", "candidate@example.com", "v" * 64)
    assert list(tmp_path.iterdir()) == [client.client_config_path]


@pytest.mark.parametrize("failure", ["missing_scope", "cross_scope", "offline", "provider"])
def test_bad_or_partial_grant_fails_without_exposing_provider_secrets(client, monkeypatch, failure):
    flow, _, gmail, _ = mock_exchange(monkeypatch)
    if failure == "missing_scope":
        flow.credentials._scopes = []
    elif failure == "cross_scope":
        flow.credentials._granted_scopes = [*oauth.SCOPES["gmail"], *oauth.SCOPES["tracker"]]
    elif failure == "offline":
        flow.credentials._refresh_token = None
    else:
        flow.fetch_token.side_effect = RuntimeError("private-token private-code")
    with pytest.raises(oauth.GoogleOAuthError) as error:
        client.exchange("gmail", "private-code", "candidate@example.com", "v" * 64)
    assert "private-" not in str(error.value)
    gmail.users().getProfile().execute.assert_not_called()


@pytest.mark.parametrize("redirect", ["http://jobs.example.com/callback", "https://user:pass@jobs.example.com/callback", "https://jobs.example.com/callback?secret=value"])
def test_remote_callback_requires_https_without_embedded_secrets(tmp_path, redirect):
    with pytest.raises(oauth.GoogleOAuthError):
        oauth.GoogleOAuthClient(tmp_path / "missing", redirect)


def test_callback_must_match_downloaded_web_client(client):
    client.redirect_uri = "https://other.example.com/callback"
    with pytest.raises(oauth.GoogleOAuthError, match="not configured"):
        client.authorization_url("gmail", "s" * 64, "candidate@example.com")


def test_invalid_account_does_not_fall_back_to_owner(client, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_GMAIL_ACCOUNT", "owner@example.com")
    with pytest.raises(oauth.GoogleOAuthError):
        client.authorization_url("gmail", "s" * 64, "")


def test_revocation_uses_official_endpoint_and_never_logs_token(client, monkeypatch):
    post = Mock(return_value=Mock())
    monkeypatch.setattr("requests.post", post)
    client.revoke({"refresh_token": "private-refresh"})
    post.assert_called_once_with("https://oauth2.googleapis.com/revoke", data={"token": "private-refresh"}, timeout=30)
    post.side_effect = RuntimeError("private-refresh")
    with pytest.raises(oauth.GoogleOAuthError) as error:
        client.revoke({"refresh_token": "private-refresh"})
    assert "private-refresh" not in str(error.value)
