"""Merge application changes into an existing tracker without clearing history."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re

from . import google_tracker as tracker
from .gmail_auth import write_private_json

DOCUMENT_COLUMNS = {8: "Open resume", 9: "Open cover letter", 11: "Open screenshot"}
LINK_FORMULA = re.compile(r'^=HYPERLINK\("(https?://[^"\s]+)",\s*"([^"\r\n]*)"\)$', re.IGNORECASE)


def normalized_row(row):
    result = list(row[:len(tracker.HEADERS)])
    result.extend([""] * (len(tracker.HEADERS) - len(result)))
    for index in (0, 1):
        result[index] = str(result[index]).lstrip("'")
    return result


def timestamp(value):
    try:
        return dt.datetime.strptime(str(value).lstrip("'"), "%d/%m/%Y %H:%M")
    except ValueError:
        return None


def web_link(value):
    text = str(value).strip()
    formula = LINK_FORMULA.fullmatch(text)
    return formula.group(1) if formula else (text if text.startswith(("https://", "http://")) else "")


def row_key(row):
    # A source URL survives application-stage and ATS URL changes.
    return (web_link(row[6]) or web_link(row[7]), str(row[3]).strip().casefold(), str(row[4]).strip().casefold())


def merge_notes(old, new):
    parts = [part.strip() for part in str(old).split(" | ") if part.strip()]
    for part in str(new).split(" | "):
        if part.strip() and part.strip() not in parts:
            parts.append(part.strip())
    return " | ".join(parts)


def merge_rows(existing, incoming):
    """Preserve unmatched history and newer sheet outcomes; reject ambiguity."""
    if existing and existing[0] != tracker.HEADERS:
        raise ValueError("Tracker headers do not match the expected 14 columns.")
    result = [list(tracker.HEADERS)] + [normalized_row(row) for row in existing[1:]]
    matched = set()
    counts = {"added": 0, "updated": 0, "kept_newer": 0, "preserved_history": 0}
    for source in incoming:
        row = normalized_row(source)
        candidates = [i for i, old in enumerate(result[1:], 1) if i not in matched and row_key(old) == row_key(row)]
        for column in (0, 5):
            if len(candidates) > 1:
                exact = [i for i in candidates if result[i][column] == row[column]]
                if exact:
                    candidates = exact
        if len(candidates) > 1:
            raise ValueError("Multiple tracker rows match an application; reconcile them before syncing.")
        if not candidates:
            result.append(row)
            matched.add(len(result) - 1)
            counts["added"] += 1
            continue
        index = candidates[0]
        matched.add(index)
        old = result[index]
        old_time, new_time = timestamp(old[1]), timestamp(row[1])
        if old_time is not None and (new_time is None or old_time > new_time):
            counts["kept_newer"] += 1
            continue
        merged = [new if new != "" else previous for previous, new in zip(old, row)]
        for column in DOCUMENT_COLUMNS:
            if web_link(old[column]) and not web_link(row[column]):
                merged[column] = old[column]
        merged[12] = merge_notes(old[12], row[12])
        if merged != old:
            result[index] = merged
            counts["updated"] += 1
    counts["preserved_history"] = sum(i not in matched for i in range(1, len(existing)))
    return result, counts


def cell_requests(sheet_id, before, after):
    """Write only changed cells; preserve existing formulas and manual fields."""
    requests = []
    for row_index, row in enumerate(after):
        previous = normalized_row(before[row_index]) if row_index < len(before) else [""] * len(tracker.HEADERS)
        for column, value in enumerate(row):
            if value == previous[column]:
                continue
            formula = LINK_FORMULA.fullmatch(str(value)) if column in DOCUMENT_COLUMNS else None
            entered = {"formulaValue": str(value)} if formula else {"stringValue": str(value)}
            requests.append({"updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": row_index, "columnIndex": column},
                "rows": [{"values": [{"userEnteredValue": entered}]}],
                "fields": "userEnteredValue",
            }})
    return requests


def read_json(path, default):
    # Corrupt state must not silently discard uploaded-file references.
    return json.loads(path.read_text()) if path.exists() else default


def newest_first_requests(sheet_id, rows):
    """Move whole rows, including formatting and formulas, by Applied At."""
    order = list(range(1, len(rows)))
    desired = sorted(order, key=lambda index: timestamp(rows[index][0]) or dt.datetime.min, reverse=True)
    requests = []
    for target, original in enumerate(desired, 1):
        source = order.index(original) + 1
        if source != target:
            requests.append({"moveDimension": {
                "source": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": source, "endIndex": source + 1},
                "destinationIndex": target,
            }})
            order.insert(target - 1, order.pop(source - 1))
    return requests, [rows[0]] + [rows[index] for index in desired]


def sync_tracker(args):
    state_dir = args.drive_state.parent
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_dir / "tracker_sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return sync_locked(args, state_dir)


def sync_locked(args, state_dir):
    sheets, drive = tracker.google_services(args.google_token)
    metadata = sheets.spreadsheets().get(spreadsheetId=args.spreadsheet_id).execute()
    gid = getattr(args, "sheet_id", None)
    matches = [s["properties"] for s in metadata.get("sheets", []) if (
        s["properties"]["sheetId"] == gid if gid is not None else s["properties"]["title"] == args.tab_name
    )]
    if len(matches) != 1:
        raise ValueError("Configured tracker tab was not found.")
    tab = matches[0]
    sheet_id = tab["sheetId"]
    quoted = "'" + tab["title"].replace("'", "''") + "'"
    values_api = sheets.spreadsheets().values()
    def read_values():
        return values_api.get(spreadsheetId=args.spreadsheet_id, range=f"{quoted}!A:N", valueRenderOption="FORMULA").execute().get("values", [])
    before = read_values()
    incoming = tracker.rows_from_db(args.db_path, args.repo_root)
    after, counts = merge_rows(before, incoming)
    summary = {"rows": len(after) - 1, **counts, "dry_run": bool(args.dry_run)}
    if args.dry_run:
        return summary

    backup = state_dir / "tracker_before_sync.json"
    uploads = read_json(args.drive_state, {"files": {}})
    # Only missing links need an upload. Old Drive links remain usable even
    # when their original files are outside the new token's drive.file grant.
    for row in after[1:]:
        for column, label in DOCUMENT_COLUMNS.items():
            if row[column] and not web_link(row[column]):
                path = tracker.abs_path(row[column], args.repo_root)
                if path and path.is_file():
                    job_key = hashlib.sha256(repr(row_key(row)).encode()).hexdigest()[:12]
                    sent_stages = {"submitted", "submission_result", "rejected", "interview_invited", "assessment_requested", "action_required", "application_progressed", "offer_received"}
                    if column in (8, 9) and row[2] not in sent_stages:
                        label = "Open prepared resume" if column == 8 else "Open prepared cover letter"
                    row[column] = tracker.upload_local_file(drive, row[column], job_key, uploads, args.drive_state, args.drive_folder_name, label, args.repo_root)
                    if not web_link(row[column]):
                        raise RuntimeError("Document upload was not confirmed; retry the tracker sync.")

    requests = cell_requests(sheet_id, before, after)
    moves, sorted_rows = newest_first_requests(sheet_id, after)
    formatting = tracker.formatting_requests(sheet_id, after)
    signature = hashlib.sha256(json.dumps(tracker.formatting_requests(sheet_id, sorted_rows), sort_keys=True).encode()).hexdigest()
    state_path = state_dir / "tracker_sync_state.json"
    state = read_json(state_path, {})
    if state.get("format_signature") != signature:
        requests.extend(formatting)
    requests.extend(moves)
    if requests:
        # Re-read immediately before mutation to detect edits made during uploads.
        if read_values() != before:
            raise ValueError("Tracker changed during preparation; retry against the latest rows.")
        write_private_json(backup, {"spreadsheet_id": args.spreadsheet_id, "sheet_id": sheet_id, "tab": tab["title"], "values": before, "metadata": metadata})
        needed_rows = len(after) - tab.get("gridProperties", {}).get("rowCount", len(after))
        if needed_rows > 0:
            requests.insert(0, {"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": needed_rows}})
        sheets.spreadsheets().batchUpdate(spreadsheetId=args.spreadsheet_id, body={"requests": requests}).execute()
    write_private_json(state_path, {"format_signature": signature, "last_success_at": dt.datetime.now(dt.timezone.utc).isoformat(), "rows": len(after) - 1})
    summary["changed_cells"] = len(cell_requests(sheet_id, before, after))
    summary["moved_rows"] = len(moves)
    return summary
