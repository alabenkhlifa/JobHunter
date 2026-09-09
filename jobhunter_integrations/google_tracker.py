#!/usr/bin/env python3
"""Sync JobHunter applications to a shared Google Sheet.

This module is open-source-safe: all account-specific values come from CLI args,
environment variables, or local ignored files. It never contains OAuth tokens,
spreadsheet IDs, email addresses, or machine-specific paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
HEADERS = [
    "Applied At",
    "Last Updated",
    "Status",
    "Job Title",
    "Company",
    "Platform",
    "Job URL",
    "Application URL",
    "Resume Sent",
    "Cover Letter Sent",
    "Package Folder",
    "Evidence Screenshot",
    "Notes",
    "Next Action",
]
HEADER_BACKGROUND = {"red": 226 / 255, "green": 236 / 255, "blue": 253 / 255}  # #E2ECFD
STATUS_PALETTE = {
    "green": {"red": 215 / 255, "green": 238 / 255, "blue": 211 / 255},  # #D7EED3
    "offer": {"red": 0.75, "green": 0.93, "blue": 0.78},
    "red": {"red": 243 / 255, "green": 212 / 255, "blue": 205 / 255},  # #F3D4CD
    "blocked": {"red": 247 / 255, "green": 225 / 255, "blue": 195 / 255},  # #F7E1C3
    "blue": {"red": 213 / 255, "green": 228 / 255, "blue": 252 / 255},  # #D5E4FC
    "amber": {"red": 1.0, "green": 0.93, "blue": 0.72},
    "purple": {"red": 230 / 255, "green": 219 / 255, "blue": 247 / 255},  # #E6DBF7
    "grey": {"red": 0.92, "green": 0.92, "blue": 0.92},
    "neutral": {"red": 0.95, "green": 0.96, "blue": 0.98},
}


def default_repo_root() -> Path:
    return Path(os.getenv("JOBHUNTER_REPO_ROOT", Path.cwd())).resolve()


def default_state_dir() -> Path:
    return Path(os.getenv("JOBHUNTER_STATE_DIR", Path.home() / ".jobhunter" / "state")).expanduser()


def default_token_path() -> Path:
    return Path(os.getenv("JOBHUNTER_TRACKER_GOOGLE_TOKEN_PATH", Path.home() / ".jobhunter" / "google_tracker_token.json")).expanduser()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return dict(default)
    save_json(path, default)
    return dict(default)


def save_json(path: Path, data: dict[str, Any]) -> None:
    from .gmail_auth import write_private_json
    write_private_json(path, data)


def format_dt(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return str(value)


def sheet_text_dt(value: str | None) -> str:
    formatted = format_dt(value)
    return f"'{formatted}" if formatted else ""


def abs_path(path: str | None, repo_root: Path) -> Path | None:
    if not path:
        return None
    p = Path(path)
    return p if p.is_absolute() else repo_root / p


def first_match(folder: Path | None, patterns: list[str]) -> str:
    if not folder or not folder.exists() or not folder.is_dir():
        return ""
    for pattern in patterns:
        matches = sorted(folder.glob(pattern))
        if matches:
            return str(matches[0])
    return ""


def next_action(stage: str, error: str | None) -> str:
    stage = stage or ""
    if stage == "submitted":
        return "Monitor Gmail / ATS replies"
    if stage == "rejected":
        return "Application closed"
    if stage == "interview_invited":
        return "Review and respond to interview invitation"
    if stage == "assessment_requested":
        return "Review and complete assessment"
    if stage == "action_required":
        return "Review requested action"
    if stage == "application_progressed":
        return "Monitor email for next steps"
    if stage == "offer_received":
        return "Review offer; acceptance requires approval"
    if stage.startswith("blocked"):
        return error or "Needs manual unblock/review"
    if stage in {"package_generated", "package_prepared", "draft_ready", "interested"}:
        return "Prepare/review application, then approve before submit"
    if stage == "skipped":
        return "No action"
    return "Review status"


def status_color(status: str) -> dict[str, float]:
    s = (status or "").strip().lower()
    if s in {"submitted", "submission_result"}:
        return STATUS_PALETTE["green"]
    if s == "offer_received":
        return STATUS_PALETTE["offer"]
    if s in {"failed", "rejected", "unavailable"}:
        return STATUS_PALETTE["red"]
    if s.startswith("blocked"):
        return STATUS_PALETTE["blocked"]
    if s in {
        "application_progressed",
        "interview_invited",
        "package_generated",
        "package_prepared",
        "draft_ready",
        "draft_inspected",
        "approved",
        "approved_to_prepare_apply",
        "resume_uploaded",
        "after_upload",
    }:
        return STATUS_PALETTE["blue"]
    if s in {"action_required", "assessment_requested"}:
        return STATUS_PALETTE["amber"]
    if s == "interested":
        return STATUS_PALETTE["purple"]
    if s in {"archived", "closed", "skipped", "withdrawn"}:
        return STATUS_PALETTE["grey"]
    return STATUS_PALETTE["neutral"]


def google_services(token_path: Path):
    from .google_tracker_auth import checked_services
    return checked_services(token_path)


def share_drive_folder(drive, folder_id: str, recipients: str) -> list[str]:
    readers = sorted({value.strip().casefold() for value in recipients.split(",") if value.strip()})
    if not readers:
        return []
    if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) for value in readers):
        raise ValueError("Configure valid tracker sharing email addresses.")
    existing = set()
    page_token = None
    while True:
        options = {"fileId": folder_id, "fields": "nextPageToken,permissions(type,emailAddress,role)", "pageSize": 100}
        if page_token:
            options["pageToken"] = page_token
        page = drive.permissions().list(**options).execute()
        for permission in page.get("permissions", []):
            if permission.get("type") == "user" and permission.get("role") in {"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"}:
                existing.add(str(permission.get("emailAddress", "")).casefold())
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    for email in readers:
        if email not in existing:
            drive.permissions().create(
                fileId=folder_id, body={"type": "user", "role": "reader", "emailAddress": email},
                sendNotificationEmail=False, fields="id",
            ).execute()
    return readers


def ensure_drive_folder(drive, state: dict[str, Any], state_path: Path, folder_name: str) -> str | None:
    if not state.get("folder_id"):
        try:
            resp = drive.files().create(
                body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
                fields="id, webViewLink",
            ).execute()
        except Exception:
            return None
        state["folder_id"] = resp.get("id")
        state["folder_link"] = resp.get("webViewLink")
        save_json(state_path, state)
    folder_id = str(state["folder_id"])
    readers = share_drive_folder(drive, folder_id, os.getenv("JOBHUNTER_TRACKER_SHARE_WITH", ""))
    if state.get("shared_with", []) != readers:
        state["shared_with"] = readers
        save_json(state_path, state)
    return folder_id


def upload_local_file(drive, file_path: str | None, job_id: str, state: dict[str, Any], state_path: Path, folder_name: str, label: str, repo_root: Path) -> str:
    path = abs_path(file_path, repo_root)
    if not path or not path.exists() or not path.is_file():
        return file_path or ""
    key = str(path)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    files_state = state.setdefault("files", {})
    if key in files_state and files_state[key].get("webViewLink") and files_state[key].get("sha256") == digest:
        return f'=HYPERLINK("{files_state[key]["webViewLink"]}", "{label}")'
    folder_id = ensure_drive_folder(drive, state, state_path, folder_name)
    if not folder_id:
        return str(path)
    from googleapiclient.http import MediaFileUpload

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
    try:
        created = drive.files().create(
            body={"name": f"{job_id} - {path.name}", "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink",
        ).execute()
    except Exception:
        return str(path)
    if not created.get("id") or not created.get("webViewLink"):
        return str(path)
    # Evidence filenames can be reused. Keep the old Drive file intact and
    # cache each newly uploaded version by its contents, not just its path.
    files_state[key] = {"id": created["id"], "webViewLink": created["webViewLink"], "sha256": digest}
    save_json(state_path, state)
    return f'=HYPERLINK("{created.get("webViewLink")}", "{label}")'


def rows_from_db(db_path: Path, repo_root: Path, drive=None, drive_state_path: Path | None = None, drive_folder_name: str = "JobHunter Application Evidence") -> list[list[Any]]:
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.job_id, a.stage, a.package_path, a.created_at, a.approved_at,
                   a.submitted_at, a.platform, a.application_url, a.notes, a.error,
                   a.application_type, a.evidence_path, j.title, j.company, j.url AS job_url
            FROM applications a
            LEFT JOIN jobs j ON j.id = a.job_id
            ORDER BY COALESCE(a.submitted_at, a.approved_at, a.created_at) DESC, a.id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    drive_state = load_json(drive_state_path, {"files": {}}) if drive and drive_state_path else {"files": {}}
    active_drive_state_path = drive_state_path or (default_state_dir() / "tracker_drive_files.json")
    output: list[list[Any]] = []
    seen_keys = set()
    for r in rows:
        package = abs_path(r["package_path"], repo_root)
        # The upload engine records the selected PDF directly; package
        # generation records its folder. Both are valid application records.
        package_file = package if package and package.is_file() else None
        package_folder = package_file.parent if package_file else package
        # A selected file does not authorize attaching unrelated siblings.
        search_folder = None if package_file else package_folder
        is_cover = bool(package_file and "cover" in package_file.stem.casefold())
        resume = str(package_file) if package_file and package_file.suffix.lower() == ".pdf" and not is_cover else first_match(search_folder, ["Resume*.pdf", "*Resume*.pdf", "resume*.pdf"])
        cover = str(package_file) if package_file and package_file.suffix.lower() == ".pdf" and is_cover else first_match(search_folder, ["CoverLetter*.pdf", "*Cover*Letter*.pdf", "cover*.pdf"])
        resume_cell = upload_local_file(drive, resume, r["job_id"], drive_state, active_drive_state_path, drive_folder_name, "Open resume", repo_root) if drive and resume else resume
        cover_cell = upload_local_file(drive, cover, r["job_id"], drive_state, active_drive_state_path, drive_folder_name, "Open cover letter", repo_root) if drive and cover else cover
        key = (r["job_id"], r["stage"], r["submitted_at"] or r["created_at"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        evidence_cell = upload_local_file(drive, r["evidence_path"], r["job_id"], drive_state, active_drive_state_path, drive_folder_name, "Open screenshot", repo_root) if drive else (r["evidence_path"] or "")
        output.append([
            sheet_text_dt(r["submitted_at"] or r["approved_at"] or r["created_at"] or ""),
            sheet_text_dt(r["created_at"] or r["submitted_at"] or ""),
            r["stage"] or "",
            r["title"] or r["job_id"],
            r["company"] or "",
            r["platform"] or "",
            r["job_url"] or "",
            r["application_url"] or "",
            resume_cell,
            cover_cell,
            str(package_folder) if package_folder else "",
            evidence_cell,
            " | ".join(x for x in [r["application_type"], r["notes"], r["error"]] if x),
            next_action(r["stage"] or "", r["error"]),
        ])
    return output


def ensure_tab(svc, spreadsheet_id: str, tab_name: str) -> int:
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_name:
            return int(props["sheetId"])
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    return int(resp["replies"][0]["addSheet"]["properties"]["sheetId"])


def formatting_requests(sheet_id: int, values: list[list[Any]]) -> list[dict[str, Any]]:
    row_count = len(values)
    requests = [
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 12}, "backgroundColor": HEADER_BACKGROUND, "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": max(row_count, 2), "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP", "textFormat": {"fontSize": 11}}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat.fontSize)"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": max(row_count, 2)}, "properties": {"pixelSize": 84}, "fields": "pixelSize"}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(HEADERS)}}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
    ]
    for row_index, row in enumerate(values[1:], start=1):
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}, "cell": {"userEnteredFormat": {"backgroundColor": status_color(str(row[2] if len(row) > 2 else ""))}}, "fields": "userEnteredFormat.backgroundColor"}})
    return requests


def sync_tracker(args: argparse.Namespace) -> dict[str, Any]:
    from .google_tracker_sync import sync_tracker as sync
    return sync(args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spreadsheet-id", default=os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID"), required=not bool(os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID")))
    parser.add_argument("--tab-name", default=os.getenv("JOBHUNTER_TRACKER_TAB", "Applications"))
    parser.add_argument("--sheet-id", type=int, default=os.getenv("JOBHUNTER_TRACKER_SHEET_ID"))
    parser.add_argument("--dry-run", action="store_true", help="Inspect the merge without writing to Sheets or Drive.")
    parser.add_argument("--repo-root", type=Path, default=default_repo_root())
    parser.add_argument("--db-path", type=Path, default=Path(os.getenv("JOBHUNTER_DB_PATH", default_repo_root() / "data" / "jobs.db")))
    parser.add_argument("--google-token", type=Path, default=default_token_path())
    parser.add_argument("--drive-state", type=Path, default=Path(os.getenv("JOBHUNTER_TRACKER_DRIVE_STATE", default_state_dir() / "tracker_drive_files.json")))
    parser.add_argument("--drive-folder-name", default=os.getenv("JOBHUNTER_TRACKER_DRIVE_FOLDER_NAME", "JobHunter Application Evidence"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv(default_repo_root() / ".env")
    try:
        result = sync_tracker(parse_args(argv))
    except Exception:
        # Provider failures can contain private URLs or token response data.
        print("Tracker sync failed; existing sheet rows are retained. Check authorization, tab headers, ambiguous matches and local state.")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
