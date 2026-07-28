#!/usr/bin/env python3
"""Sync JobHunter applications to a shared Google Sheet.

This module is open-source-safe: all account-specific values come from CLI args,
environment variables, or local ignored files. It never contains OAuth tokens,
spreadsheet IDs, email addresses, or machine-specific paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import sqlite3
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
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


def default_repo_root() -> Path:
    return Path(os.getenv("JOBHUNTER_REPO_ROOT", Path.cwd())).resolve()


def default_state_dir() -> Path:
    return Path(os.getenv("JOBHUNTER_STATE_DIR", Path.home() / ".jobhunter" / "state")).expanduser()


def default_token_path() -> Path:
    return Path(os.getenv("GOOGLE_TOKEN_PATH", Path.home() / ".jobhunter" / "google_token.json")).expanduser()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return dict(default)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(default, indent=2, sort_keys=True))
    return dict(default)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


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
    s = (status or "").lower()
    if s in {"submitted", "offer_received"}:
        return {"red": 0.82, "green": 0.94, "blue": 0.82}
    if s.startswith("blocked") or s in {"failed", "rejected", "unavailable"}:
        return {"red": 0.98, "green": 0.83, "blue": 0.80}
    if s in {
        "application_progressed",
        "interview_invited",
        "package_generated",
        "package_prepared",
        "draft_ready",
        "approved",
    }:
        return {"red": 0.82, "green": 0.90, "blue": 1.0}
    if s in {"action_required", "assessment_requested"}:
        return {"red": 1.0, "green": 0.93, "blue": 0.72}
    if s == "interested":
        return {"red": 0.91, "green": 0.86, "blue": 0.98}
    if s == "skipped":
        return {"red": 0.92, "green": 0.92, "blue": 0.92}
    return {"red": 1.0, "green": 1.0, "blue": 1.0}


def google_services(token_path: Path):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    return (
        build("sheets", "v4", credentials=creds, cache_discovery=False),
        build("drive", "v3", credentials=creds, cache_discovery=False),
    )


def ensure_drive_folder(drive, state: dict[str, Any], state_path: Path, folder_name: str) -> str | None:
    if state.get("folder_id"):
        return str(state["folder_id"])
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
    return state.get("folder_id")


def upload_local_file(drive, file_path: str | None, job_id: str, state: dict[str, Any], state_path: Path, folder_name: str, label: str, repo_root: Path) -> str:
    path = abs_path(file_path, repo_root)
    if not path or not path.exists() or not path.is_file():
        return file_path or ""
    key = str(path)
    files_state = state.setdefault("files", {})
    if key in files_state and files_state[key].get("webViewLink"):
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
    files_state[key] = {"id": created.get("id"), "webViewLink": created.get("webViewLink")}
    save_json(state_path, state)
    return f'=HYPERLINK("{created.get("webViewLink")}", "{label}")'


def rows_from_db(db_path: Path, repo_root: Path, drive=None, drive_state_path: Path | None = None, drive_folder_name: str = "JobHunter Application Evidence") -> list[list[Any]]:
    conn = sqlite3.connect(db_path)
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
    now = sheet_text_dt(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    output: list[list[Any]] = []
    seen_keys = set()
    for r in rows:
        package = abs_path(r["package_path"], repo_root)
        resume = first_match(package, ["Resume*.pdf", "*Resume*.pdf", "resume*.pdf"])
        cover = first_match(package, ["CoverLetter*.pdf", "*Cover*Letter*.pdf", "cover*.pdf"])
        resume_cell = upload_local_file(drive, resume, r["job_id"], drive_state, active_drive_state_path, drive_folder_name, "Open resume", repo_root) if drive and resume else resume
        cover_cell = upload_local_file(drive, cover, r["job_id"], drive_state, active_drive_state_path, drive_folder_name, "Open cover letter", repo_root) if drive and cover else cover
        key = (r["job_id"], r["stage"], r["submitted_at"] or r["created_at"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        evidence_cell = upload_local_file(drive, r["evidence_path"], r["job_id"], drive_state, active_drive_state_path, drive_folder_name, "Open screenshot", repo_root) if drive else (r["evidence_path"] or "")
        output.append([
            sheet_text_dt(r["submitted_at"] or r["approved_at"] or r["created_at"] or ""),
            now,
            r["stage"] or "",
            r["title"] or r["job_id"],
            r["company"] or "",
            r["platform"] or "",
            r["job_url"] or "",
            r["application_url"] or "",
            resume_cell,
            cover_cell,
            str(package) if package else "",
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
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 12}, "backgroundColor": {"red": 0.88, "green": 0.93, "blue": 1.0}, "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": max(row_count, 2), "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP", "textFormat": {"fontSize": 11}}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat.fontSize)"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 1, "endIndex": max(row_count, 2)}, "properties": {"pixelSize": 84}, "fields": "pixelSize"}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(HEADERS)}}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
    ]
    for row_index, row in enumerate(values[1:], start=1):
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}, "cell": {"userEnteredFormat": {"backgroundColor": status_color(str(row[2] if len(row) > 2 else ""))}}, "fields": "userEnteredFormat.backgroundColor"}})
    return requests


def sync_tracker(args: argparse.Namespace) -> dict[str, Any]:
    sheets, drive = google_services(args.google_token)
    sheet_id = ensure_tab(sheets, args.spreadsheet_id, args.tab_name)
    values = [HEADERS] + rows_from_db(args.db_path, args.repo_root, drive, args.drive_state, args.drive_folder_name)
    sheets.spreadsheets().values().clear(spreadsheetId=args.spreadsheet_id, range=f"{args.tab_name}!A:N", body={}).execute()
    sheets.spreadsheets().values().update(spreadsheetId=args.spreadsheet_id, range=f"{args.tab_name}!A1", valueInputOption="USER_ENTERED", body={"values": values}).execute()
    sheets.spreadsheets().batchUpdate(spreadsheetId=args.spreadsheet_id, body={"requests": formatting_requests(sheet_id, values)}).execute()
    drive_state = load_json(args.drive_state, {"files": {}})
    return {"spreadsheet_id": args.spreadsheet_id, "tab": args.tab_name, "rows": len(values) - 1, "uploaded_files": len(drive_state.get("files", {})), "drive_folder_link": drive_state.get("folder_link", "")}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spreadsheet-id", default=os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID"), required=not bool(os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID")))
    parser.add_argument("--tab-name", default=os.getenv("JOBHUNTER_TRACKER_TAB", "Applications"))
    parser.add_argument("--repo-root", type=Path, default=default_repo_root())
    parser.add_argument("--db-path", type=Path, default=Path(os.getenv("JOBHUNTER_DB_PATH", default_repo_root() / "data" / "jobs.db")))
    parser.add_argument("--google-token", type=Path, default=default_token_path())
    parser.add_argument("--drive-state", type=Path, default=Path(os.getenv("JOBHUNTER_TRACKER_DRIVE_STATE", default_state_dir() / "tracker_drive_files.json")))
    parser.add_argument("--drive-folder-name", default=os.getenv("JOBHUNTER_TRACKER_DRIVE_FOLDER_NAME", "JobHunter Application Evidence"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    result = sync_tracker(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
