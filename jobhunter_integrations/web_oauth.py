"""Google web consent adapter; identity, state lifetime and storage stay explicit.

The caller must bind state, account, kind and PKCE verifier to one candidate and
consume that state exactly once before exchange. No credentials are persisted
or printed automatically. This adapter never loads owner token/account defaults.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit

from . import gmail_auth, google_tracker_auth
from .google_tracker import SCOPES as TRACKER_SCOPES

GoogleOAuthError = gmail_auth.GmailAuthError
SCOPES = {"gmail": tuple(gmail_auth.SCOPES), "tracker": tuple(TRACKER_SCOPES)}
_LOG_LOCK = threading.RLock()
_SENSITIVE_LOGGERS = (
    "google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session",
    "oauthlib.oauth2.rfc6749.clients.base", "urllib3.connectionpool",
    "google.auth.transport.requests", "googleapiclient.http",
)


@contextmanager
def _private_exchange():
    # OAuth library debug logs include codes, request bodies and credentials.
    with _LOG_LOCK:
        loggers = [logging.getLogger(name) for name in _SENSITIVE_LOGGERS]
        levels = [logger.level for logger in loggers]
        for logger in loggers:
            logger.setLevel(logging.CRITICAL + 1)
        try:
            yield
        finally:
            for logger, level in zip(loggers, levels):
                logger.setLevel(level)


@dataclass(frozen=True)
class VerifiedGoogleGrant:
    kind: str
    account: str
    token: dict = field(repr=False)

    def persist(self, token_path: Path) -> None:
        gmail_auth.write_private_json(Path(token_path), self.token)


class GoogleOAuthClient:
    def __init__(self, client_config_path: Path, redirect_uri: str):
        self.client_config_path = Path(client_config_path)
        self.redirect_uri = redirect_uri
        parsed = urlsplit(redirect_uri)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or not (parsed.scheme == "https" or local_http)
                or parsed.username or parsed.password or parsed.fragment or parsed.query):
            raise GoogleOAuthError("Configure an HTTPS OAuth callback, or a loopback callback for local testing.")

    def _flow(self, kind: str, code_verifier: str):
        from google_auth_oauthlib.flow import Flow

        if kind not in SCOPES:
            raise GoogleOAuthError("Unknown Google authorization purpose.")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier or ""):
            raise GoogleOAuthError("The authorization session is invalid; start consent again.")
        try:
            web = json.loads(self.client_config_path.expanduser().read_text())["web"]
            if not web.get("client_id") or not web.get("client_secret") or self.redirect_uri not in web.get("redirect_uris", []):
                raise ValueError()
            config = {"web": {
                "client_id": web["client_id"], "client_secret": web["client_secret"],
                "auth_uri": gmail_auth.AUTH_URI, "token_uri": gmail_auth.TOKEN_URI,
                "redirect_uris": [self.redirect_uri],
            }}
            return Flow.from_client_config(
                config, list(SCOPES[kind]), redirect_uri=self.redirect_uri,
                code_verifier=code_verifier, autogenerate_code_verifier=False,
            )
        except GoogleOAuthError:
            raise
        except Exception:
            raise GoogleOAuthError("Google web authorization is not configured for this callback.") from None

    def authorization_url(self, kind: str, state: str, account: str) -> dict[str, str]:
        account = gmail_auth.normalize_account(account)
        if not isinstance(state, str) or len(state) < 32 or len(state) > 512:
            raise GoogleOAuthError("The authorization session is invalid; start consent again.")
        verifier = secrets.token_urlsafe(64)
        try:
            with _private_exchange():
                flow = self._flow(kind, verifier)
                url, _ = flow.authorization_url(
                    state=state, access_type="offline", prompt="consent", login_hint=account,
                    include_granted_scopes="false",
                )
            return {"url": url, "code_verifier": verifier}
        except GoogleOAuthError:
            raise
        except Exception:
            raise GoogleOAuthError("Google consent could not be started; retry later.") from None

    def exchange(self, kind: str, code: str, account: str, code_verifier: str) -> VerifiedGoogleGrant:
        account = gmail_auth.normalize_account(account)
        if not isinstance(code, str) or not code or len(code) > 8192:
            raise GoogleOAuthError("Google consent did not provide an authorization code.")
        try:
            with _private_exchange():
                flow = self._flow(kind, code_verifier)
                flow.fetch_token(code=code, timeout=30)
                credentials = flow.credentials
                if not credentials.refresh_token or not credentials.has_scopes(SCOPES[kind]):
                    raise GoogleOAuthError("Google did not grant the requested permissions and offline access; authorize again.")
                granted = credentials.granted_scopes
                if granted is not None and (not isinstance(granted, (list, tuple, set)) or set(granted) != set(SCOPES[kind])):
                    raise GoogleOAuthError("Google returned permissions for a different purpose; start separate consent again.")
                if kind == "gmail":
                    gmail_auth.verify_account(gmail_auth.service_for_credentials(credentials), account)
                else:
                    google_tracker_auth.verify_account(google_tracker_auth.services_for_credentials(credentials)[1], account)
                token = json.loads(credentials.to_json())
                return VerifiedGoogleGrant(kind=kind, account=account, token=token)
        except GoogleOAuthError:
            raise
        except Exception:
            raise GoogleOAuthError("Google authorization did not complete; retry consent and verify the selected account.") from None

    def revoke(self, token: dict | VerifiedGoogleGrant) -> None:
        """Revoke Google project access for this account, across grant kinds.

        Google revocation invalidates every grant for that user/project, even
        with separate tokens. Callers must invalidate matching local connections.
        """
        import requests

        value = token.token if isinstance(token, VerifiedGoogleGrant) else token
        credential = value.get("refresh_token") or value.get("token") if isinstance(value, dict) else None
        if not isinstance(credential, str) or not credential:
            raise GoogleOAuthError("There is no saved Google grant to disconnect.")
        try:
            with _private_exchange():
                response = requests.post("https://oauth2.googleapis.com/revoke", data={"token": credential}, timeout=30)
                response.raise_for_status()
        except Exception:
            raise GoogleOAuthError("Google could not confirm disconnection; retry before removing the saved grant.") from None
