"""Authorize and check the dedicated mailbox without reading or changing mail."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailAuthError(RuntimeError):
    """A safe-to-display authorization failure, without provider response data."""


def default_token_path() -> Path:
    return Path(os.getenv("GOOGLE_TOKEN_PATH", "~/.jobhunter/google_token.json")).expanduser()


def default_account_path() -> Path:
    return Path(os.getenv("JOBHUNTER_GOOGLE_ACCOUNT_CONFIG", "~/.jobhunter/google_account.json")).expanduser()


def write_private_json(path: Path, value: dict) -> None:
    """Replace a private file atomically; failed writes preserve the old file."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def expected_account(value: str | None = None, config_path: Path | None = None) -> str:
    account = value or os.getenv("JOBHUNTER_GMAIL_ACCOUNT")
    if not account:
        try:
            account = json.loads((config_path or default_account_path()).read_text())["email"]
        except (OSError, ValueError, KeyError, TypeError):
            raise GmailAuthError("Set JOBHUNTER_GMAIL_ACCOUNT or authorize with --account first.") from None
    if not isinstance(account, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", account.strip()):
        raise GmailAuthError("The expected Gmail account must be an email address.")
    return account.strip().casefold()


def service_for_credentials(credentials):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    return build("gmail", "v1", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)


def verify_account(service, account: str) -> None:
    profile = service.users().getProfile(userId="me").execute()
    if str(profile.get("emailAddress", "")).casefold() != account.casefold():
        raise GmailAuthError("Authorized mailbox does not match the configured jobs account; access stopped.")


def gmail_service(token_path: Path, account: str | None = None, *, force_refresh: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    expected = expected_account(account)
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path.expanduser()))
        if not credentials.has_scopes(SCOPES):
            raise GmailAuthError("Gmail read permission is missing; run authorize again.")
        if not credentials.refresh_token:
            raise GmailAuthError("Offline access is missing; run authorize again.")
        refreshed = force_refresh or not credentials.valid
        if refreshed:
            credentials.refresh(Request())
        service = service_for_credentials(credentials)
        verify_account(service, expected)
        if refreshed:
            write_private_json(token_path, json.loads(credentials.to_json()))
        return service
    except GmailAuthError:
        raise
    except Exception:
        raise GmailAuthError("Gmail authorization check failed. Check connectivity and Gmail API access; reauthorize if access was revoked or expired.") from None


def authorize_google_credentials(args, scopes, verify_credentials, *, success_message) -> None:
    """Run the shared Desktop consent flow; persist only verified credentials."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    account = expected_account(args.account)
    # The OAuth library logs callback URLs at INFO and token exchanges at DEBUG.
    # Keep authorization codes/tokens out of an embedding agent's configured log.
    loggers = [logging.getLogger(name) for name in ("google_auth_oauthlib.flow", "requests_oauthlib.oauth2_session", "oauthlib.oauth2.rfc6749.clients.base", "urllib3.connectionpool")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
    try:
        client = json.loads(args.client_secret.expanduser().read_text())
        installed = client.get("installed", {})
        if not installed.get("client_id") or not installed.get("client_secret"):
            raise GmailAuthError("Use the downloaded Google OAuth Desktop app client JSON.")
        # Never send credentials to endpoints supplied by an arbitrary config file.
        installed["auth_uri"] = AUTH_URI
        installed["token_uri"] = TOKEN_URI
        flow = InstalledAppFlow.from_client_config({"installed": installed}, scopes, autogenerate_code_verifier=True)
        credentials = flow.run_local_server(
            host="127.0.0.1", port=args.port, open_browser=not args.no_browser,
            timeout_seconds=300, access_type="offline", prompt="consent",
            login_hint=account,
            authorization_prompt_message="Open this Google consent link in your browser:\n{url}",
            success_message=success_message,
        )
        if not credentials.refresh_token or not credentials.has_scopes(scopes):
            raise GmailAuthError("Google did not grant the requested permissions and offline access; authorize again.")
        verify_credentials(credentials, account)
        write_private_json(args.google_token, json.loads(credentials.to_json()))
        write_private_json(default_account_path(), {"email": account})
    except GmailAuthError:
        raise
    except Exception:
        raise GmailAuthError("Google authorization did not complete. Check the Desktop client, API access, consent and local callback connection, then retry.") from None
    finally:
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)


def authorize(args) -> None:
    authorize_google_credentials(
        args, SCOPES,
        lambda credentials, account: verify_account(service_for_credentials(credentials), account),
        success_message="Google consent received. Return to the terminal for the mailbox check.",
    )


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("authorize", "check"))
    parser.add_argument("--account", default=os.getenv("JOBHUNTER_GMAIL_ACCOUNT"))
    parser.add_argument("--google-token", type=Path, default=default_token_path())
    parser.add_argument("--client-secret", type=Path, default=Path(os.getenv("GOOGLE_CLIENT_SECRET_PATH", "~/.jobhunter/google_client_secret.json")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="For SSH with a local port forward to the loopback callback.")
    parser.add_argument("--refresh", action="store_true", help="On check, exercise offline refresh even if the access token is still valid.")
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            authorize(args)
        else:
            gmail_service(args.google_token, args.account, force_refresh=args.refresh)
    except GmailAuthError as exc:
        print(f"Gmail unavailable: {exc}", file=sys.stderr)
        return 1
    print("Gmail ready: expected mailbox verified; read access and offline credentials available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
