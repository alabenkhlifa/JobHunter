"""Authorize Sheets/Drive separately and inspect an existing tracker without edits."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .gmail_auth import (
    GmailAuthError,
    authorize_google_credentials,
    default_token_path as gmail_token_path,
    expected_account as legacy_expected_account,
    normalize_account,
    write_private_json,
)
from .google_tracker import HEADERS, SCOPES


def default_token_path() -> Path:
    return Path(os.getenv("JOBHUNTER_TRACKER_GOOGLE_TOKEN_PATH", "~/.jobhunter/google_tracker_token.json")).expanduser()


def default_account_path() -> Path:
    return Path(os.getenv("JOBHUNTER_TRACKER_ACCOUNT_CONFIG", "~/.jobhunter/google_tracker_account.json")).expanduser()


def expected_account(value: str | None = None, config_path: Path | None = None) -> str:
    if value is not None:
        return normalize_account(value)
    if config_path is None and os.getenv("JOBHUNTER_TRACKER_ACCOUNT"):
        return normalize_account(os.environ["JOBHUNTER_TRACKER_ACCOUNT"])
    path = config_path or default_account_path()
    if path.exists() or config_path is not None or os.getenv("JOBHUNTER_TRACKER_ACCOUNT_CONFIG"):
        try:
            return normalize_account(json.loads(path.read_text())["email"])
        except (OSError, ValueError, KeyError, TypeError):
            raise GmailAuthError("Set the tracker account or authorize it first.") from None
    # Only the original owner workflow may use its pre-existing mailbox setting.
    return legacy_expected_account()


def require_separate_token(path: Path) -> None:
    if path.expanduser().resolve() == gmail_token_path().resolve():
        raise GmailAuthError("Use a separate tracker token path; the mailbox token must be preserved.")


def services_for_credentials(credentials):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    def service(name, version):
        return build(name, version, http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)), cache_discovery=False)

    return service("sheets", "v4"), service("drive", "v3")


def verify_account(drive, account: str) -> None:
    profile = drive.about().get(fields="user(emailAddress)").execute()
    actual = str(profile.get("user", {}).get("emailAddress", ""))
    if actual.casefold() != account.casefold():
        raise GmailAuthError("Authorized Drive account does not match the configured tracker account; access stopped.")


def authorize(args) -> None:
    require_separate_token(args.google_token)
    authorize_google_credentials(
        args, SCOPES,
        lambda credentials, account: verify_account(services_for_credentials(credentials)[1], account),
        success_message="Google consent received. Return to the terminal for the tracker access check.",
        account_resolver=lambda account: expected_account(account, getattr(args, "account_config", None)),
        account_config_path=getattr(args, "account_config", None) or default_account_path(),
    )


def checked_services(token_path: Path, account: str | None = None, *, force_refresh: bool = False, account_config_path: Path | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    require_separate_token(token_path)
    expected = expected_account(account, account_config_path)
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path.expanduser()))
        if not credentials.has_scopes(SCOPES) or not credentials.refresh_token:
            raise GmailAuthError("Sheets/Drive permissions or offline access are missing; run google_tracker_auth authorize.")
        refreshed = force_refresh or not credentials.valid
        if refreshed:
            credentials.refresh(Request())
        sheets, drive = services_for_credentials(credentials)
        verify_account(drive, expected)
        if refreshed:
            write_private_json(token_path, json.loads(credentials.to_json()))
        return sheets, drive
    except GmailAuthError:
        raise
    except Exception:
        raise GmailAuthError("Tracker authorization check failed. Check Sheets/Drive API access, connectivity and the saved grant.") from None


def inspect_tracker(sheets, spreadsheet_id: str, sheet_id: int | None = None) -> dict:
    """Read metadata and headers only; never create, clear, upload or update."""
    metadata = sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="properties(title),sheets(properties(sheetId,title,sheetType))",
    ).execute()
    tabs = [item["properties"] for item in metadata.get("sheets", [])]
    selected = tabs if sheet_id is None else [tab for tab in tabs if tab["sheetId"] == sheet_id]
    if not selected:
        raise GmailAuthError("The requested tracker tab was not found; check its gid.")
    result = {"title": metadata.get("properties", {}).get("title", ""), "read_access": True, "tabs": []}
    for tab in selected:
        if tab.get("sheetType", "GRID") != "GRID":
            raise GmailAuthError("The requested tracker tab is not a grid sheet.")
        quoted = "'" + tab["title"].replace("'", "''") + "'"
        values = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{quoted}!A1:N1",
            valueRenderOption="FORMULA",
        ).execute().get("values", [])
        headers = values[0] if values else []
        result["tabs"].append({
            "sheet_id": tab["sheetId"], "title": tab["title"],
            "headers": headers, "expected_headers": headers == HEADERS,
        })
    return result


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("authorize", "check"))
    parser.add_argument("--account")
    parser.add_argument("--account-config", type=Path)
    parser.add_argument("--google-token", type=Path, default=default_token_path())
    parser.add_argument("--client-secret", type=Path, default=Path(os.getenv("GOOGLE_CLIENT_SECRET_PATH", "~/.jobhunter/google_client_secret.json")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--spreadsheet-id", default=os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID"))
    parser.add_argument("--sheet-id", type=int, default=os.getenv("JOBHUNTER_TRACKER_SHEET_ID"))
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            authorize(args)
        sheets, _ = checked_services(args.google_token, args.account, force_refresh=args.refresh, account_config_path=args.account_config)
        if args.spreadsheet_id:
            print(json.dumps(inspect_tracker(sheets, args.spreadsheet_id, args.sheet_id), indent=2))
        else:
            print("Tracker credentials ready: expected account verified; Sheets/Drive and offline access available.")
    except GmailAuthError as exc:
        print(f"Tracker unavailable: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Tracker unavailable: cannot read the requested spreadsheet/tab. Check sharing and Sheets API access.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
