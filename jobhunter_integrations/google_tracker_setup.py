"""Provision a candidate-owned tracker and evidence folder with explicit sharing.

Call only after the candidate confirms creation/connection and any viewer.
State is private and durable. A lost create response is recovered by an app
property; when the result cannot be found, stop rather than create duplicates.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import secrets

from . import google_tracker as tracker
from .google_tracker_auth import checked_services
from .gmail_auth import GmailAuthError, normalize_account, write_private_json

SHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,mimeType,trashed,owners(emailAddress),webViewLink,capabilities(canShare)"


def _owned_file(drive, file_id: str, account: str, mime_type: str) -> dict:
    result = drive.files().get(fileId=file_id, fields=FILE_FIELDS).execute()
    owners = {str(owner.get("emailAddress", "")).casefold() for owner in result.get("owners", [])}
    if (account not in owners or result.get("mimeType") != mime_type or result.get("trashed")
            or not result.get("capabilities", {}).get("canShare")):
        raise GmailAuthError("The tracker and evidence folder must be owned and shareable by the authorized tracker account.")
    return result


def _ensure_resource(drive, state, state_path, kind, name, mime_type):
    resource = state.setdefault("resources", {}).setdefault(kind, {})
    if resource.get("id"):
        return resource["id"]
    marker = f"{state['operation_id']}-{kind}"
    recovered = drive.files().list(
        q=("trashed = false and 'me' in owners and appProperties has "
           f"{{ key='jobhunter_provision' and value='{marker}' }}"),
        spaces="drive", pageSize=100, fields="files(id),nextPageToken",
    ).execute()
    found = recovered.get("files", [])
    if len(found) > 1 or recovered.get("nextPageToken"):
        raise GmailAuthError("Multiple resources match this tracker setup; reconcile them before retrying.")
    if found:
        resource["id"] = found[0]["id"]
    else:
        if resource.get("attempted"):
            raise GmailAuthError("A prior Google create request has an uncertain result; retry recovery later before creating another tracker.")
        resource["attempted"] = True
        write_private_json(state_path, state)
        created = drive.files().create(
            body={"name": name, "mimeType": mime_type,
                  "appProperties": {"jobhunter_provision": marker}}, fields="id",
        ).execute()
        if not created.get("id"):
            raise GmailAuthError("Google did not confirm tracker creation; retry recovery later.")
        resource["id"] = created["id"]
    write_private_json(state_path, state)
    return resource["id"]


def _select_tab(sheets, spreadsheet_id, sheet_id, *, new):
    metadata = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    tabs = [s["properties"] for s in metadata.get("sheets", [])
            if s["properties"].get("sheetType", "GRID") == "GRID"]
    if sheet_id is not None:
        tabs = [tab for tab in tabs if tab["sheetId"] == sheet_id]
    elif new:
        tabs = tabs[:1]
    matches = []
    for tab in tabs:
        quoted = "'" + tab["title"].replace("'", "''") + "'"
        values = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{quoted}!A1:N1", valueRenderOption="FORMULA",
        ).execute().get("values", [])
        headers = values[0] if values else []
        if headers == tracker.HEADERS or (new and not headers):
            matches.append((tab, headers))
    if len(matches) != 1:
        raise GmailAuthError("Select one tracker tab with the expected 14 columns; existing history was retained.")
    return matches[0]


def provision_tracker(token_path: Path, account: str, state_path: Path, *,
                      spreadsheet_id: str | None = None, sheet_id: int | None = None,
                      title: str = "Job Applications", viewer_email: str | None = None,
                      drive_state_path: Path | None = None) -> dict:
    """Create or connect one tracker, then share sheet AND folder as readers.

    Existing spreadsheets must already be accessible through this app's Drive
    grant (for example selected using Google Picker) and retain all existing
    cells/formatting. No global accounts, tokens, recipients or state are read.
    """
    account = normalize_account(account)
    viewer = normalize_account(viewer_email) if viewer_email is not None else None
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise GmailAuthError("Choose a tracker title between 1 and 200 characters.")
    if spreadsheet_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
        raise GmailAuthError("Provide a valid existing spreadsheet ID.")
    if sheet_id is not None and (isinstance(sheet_id, bool) or not isinstance(sheet_id, int) or sheet_id < 0):
        raise GmailAuthError("Provide a valid tracker tab ID.")
    token_path, state_path = Path(token_path), Path(state_path)
    if token_path.expanduser().resolve() == state_path.expanduser().resolve():
        raise GmailAuthError("Tracker setup state and credentials must use separate private paths.")
    if drive_state_path is not None:
        drive_state_path = Path(drive_state_path)
        if drive_state_path.resolve() in {token_path.resolve(), state_path.resolve()}:
            raise GmailAuthError("Document upload state must use a separate private path.")
        try:
            upload_state = json.loads(drive_state_path.read_text()) if drive_state_path.exists() else {}
            if upload_state.get("account", account) != account:
                raise GmailAuthError("Existing document upload state belongs to another account.")
        except (OSError, ValueError, TypeError, AttributeError):
            raise GmailAuthError("Existing document upload state could not be read; restore it before reconnecting.") from None
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _provision_locked(token_path, account, state_path, spreadsheet_id, sheet_id,
                                     title.strip(), viewer, drive_state_path)
    except GmailAuthError:
        raise
    except Exception:
        raise GmailAuthError("Tracker setup was not confirmed. Check account access and retry using the same setup state; existing history was retained.") from None


def _provision_locked(token_path, account, state_path, spreadsheet_id, sheet_id, title, viewer, drive_state_path):
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "version": 1, "account": account, "operation_id": secrets.token_hex(16),
        "existing_spreadsheet_id": spreadsheet_id, "requested_sheet_id": sheet_id,
        "resources": {},
    }
    if (state.get("version") != 1 or state.get("account") != account
            or state.get("existing_spreadsheet_id") != spreadsheet_id
            or state.get("requested_sheet_id") != sheet_id
            or not re.fullmatch(r"[a-f0-9]{32}", state.get("operation_id", ""))):
        raise GmailAuthError("Tracker setup state belongs to a different account or tracker; reconnect it explicitly.")
    sheets, drive = checked_services(token_path, account)
    write_private_json(state_path, state)
    new = spreadsheet_id is None
    spreadsheet_id = spreadsheet_id or _ensure_resource(drive, state, state_path, "spreadsheet", title, SHEET_MIME)
    _owned_file(drive, spreadsheet_id, account, SHEET_MIME)
    tab, headers = _select_tab(sheets, spreadsheet_id, sheet_id, new=new)
    if new and not state.get("initialized"):
        # A retry after initialization only reapplies formatting, never rows.
        requests = tracker.formatting_requests(tab["sheetId"], [tracker.HEADERS])
        if not headers:
            requests.insert(0, {"updateCells": {
                "start": {"sheetId": tab["sheetId"], "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": [{"userEnteredValue": {"stringValue": header}} for header in tracker.HEADERS]}],
                "fields": "userEnteredValue",
            }})
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
        state["initialized"] = True
        write_private_json(state_path, state)
    folder_id = _ensure_resource(drive, state, state_path, "folder", f"{title} Evidence", FOLDER_MIME)
    _owned_file(drive, folder_id, account, FOLDER_MIME)
    recipients = viewer if viewer and viewer != account else ""
    tracker.share_drive_folder(drive, spreadsheet_id, recipients)
    tracker.share_drive_folder(drive, folder_id, recipients)
    shared = sorted(set(state.get("shared_with", [])) | ({viewer} if recipients else set()))
    state["shared_with"] = shared
    state["complete"] = True
    write_private_json(state_path, state)
    if drive_state_path is not None:
        drive_state_path = Path(drive_state_path)
        if drive_state_path.resolve() in {token_path.resolve(), state_path.resolve()}:
            raise GmailAuthError("Document upload state must use a separate private path.")
        upload_state = json.loads(drive_state_path.read_text()) if drive_state_path.exists() else {"files": {}}
        if upload_state.get("account", account) != account or upload_state.get("folder_id", folder_id) != folder_id:
            raise GmailAuthError("Existing document upload state belongs to another account or folder.")
        upload_state.update(account=account, folder_id=folder_id,
                            folder_link=f"https://drive.google.com/drive/folders/{folder_id}",
                            shared_with=shared, share_with=shared)
        write_private_json(drive_state_path, upload_state)
    return {"spreadsheet_id": spreadsheet_id, "sheet_id": tab["sheetId"],
            "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={tab['sheetId']}",
            "drive_folder_id": folder_id, "shared_with": shared}
