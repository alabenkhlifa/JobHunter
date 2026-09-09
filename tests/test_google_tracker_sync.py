import argparse
import json
import sqlite3
from unittest.mock import Mock

import pytest

from jobhunter_integrations import google_tracker as tracker
from jobhunter_integrations import google_tracker_sync as sync


def row(name="role", stage="submitted", applied="01/09/2026 10:00", updated="01/09/2026 10:00", platform="ATS"):
    return [applied, updated, stage, name, "Example", platform, f"https://example.com/jobs/{name}", "", "", "", "", "", "", "Monitor replies"]


def test_preserves_history_newer_rejection_and_links_when_db_is_stale():
    rejected = row(stage="rejected", updated="02/09/2026 10:00")
    rejected[8] = '=HYPERLINK("https://drive.google.com/existing", "Open resume")'
    history = row("history")
    after, counts = sync.merge_rows([tracker.HEADERS, rejected, history], [row(), row("new")])
    assert after[1:3] == [rejected, history]
    assert counts == {"added": 1, "updated": 0, "kept_newer": 1, "preserved_history": 1}


def test_new_outcome_updates_once_preserving_links_and_manual_notes():
    old = row()
    old[8] = '=HYPERLINK("https://drive.google.com/existing", "Open resume")'
    old[12] = "Contacted recruiter"
    new = row(stage="interview_invited", updated="02/09/2026 10:00")
    new[8], new[12] = "/missing/resume.pdf", "Interview detected"
    before = [tracker.HEADERS, old]
    after, counts = sync.merge_rows(before, [new])
    assert after[1][2] == "interview_invited"
    assert after[1][8] == old[8]
    assert after[1][12] == "Contacted recruiter | Interview detected"
    assert counts["updated"] == 1
    again, counts = sync.merge_rows(after, [new])
    assert again == after
    assert counts["updated"] == counts["added"] == 0


@pytest.mark.parametrize("source_kind", ["file", "missing", "directory"])
def test_evidence_replaces_old_link_only_when_current_file_is_available(tmp_path, source_kind):
    old = row()
    old[8] = '=HYPERLINK("https://drive.google.com/resume", "Open resume")'
    old[11] = '=HYPERLINK("https://drive.google.com/form", "Open screenshot")'
    path = tmp_path / "evidence.png"
    if source_kind == "file":
        path.write_bytes(b"confirmation screenshot")
    elif source_kind == "directory":
        path.mkdir()
    new = row(updated="02/09/2026 10:00")
    new[8], new[11] = "resume.pdf", "evidence.png"
    after, _ = sync.merge_rows([tracker.HEADERS, old], [new], repo_root=tmp_path)
    assert after[1][8] == old[8]
    assert after[1][11] == (new[11] if source_kind == "file" else old[11])


def test_stale_database_cannot_replace_newer_sheet_evidence(tmp_path):
    old = row(updated="02/09/2026 10:00")
    old[11] = '=HYPERLINK("https://drive.google.com/confirmation", "Open screenshot")'
    path = tmp_path / "form.png"
    path.write_bytes(b"old form")
    new = row()
    new[11] = str(path)
    after, counts = sync.merge_rows([tracker.HEADERS, old], [new], repo_root=tmp_path)
    assert after[1] == old
    assert counts["kept_newer"] == 1


def test_duplicate_job_history_matches_by_date_then_platform():
    first = row(stage="package_generated", platform="LinkedIn")
    second = row(stage="rejected", applied="02/09/2026 10:00", updated="03/09/2026 10:00")
    incoming = row(applied="02/09/2026 10:00", updated="02/09/2026 10:00")
    result, counts = sync.merge_rows([tracker.HEADERS, first, second], [incoming])
    assert result[1:] == [first, second]
    assert counts["kept_newer"] == 1
    changed = row(stage="interview_invited", applied="04/09/2026 10:00", updated="04/09/2026 10:00")
    result, _ = sync.merge_rows(result, [changed])
    assert result[1] == first
    assert result[2][2] == "interview_invited"


def test_ambiguous_matches_and_wrong_headers_fail_before_changes():
    with pytest.raises(ValueError, match="Multiple tracker rows"):
        sync.merge_rows([tracker.HEADERS, row(), row()], [row()])
    with pytest.raises(ValueError, match="headers"):
        sync.merge_rows([["Custom", "Sheet"]], [row()])


def test_changed_cells_treat_untrusted_text_as_literal_and_keep_hyperlinks():
    new = row("=IMPORTXML(\"https://untrusted.example\",\"//x\")")
    new[8] = '=HYPERLINK("https://drive.google.com/new", "Open resume")'
    requests = sync.cell_requests(123, [tracker.HEADERS], [tracker.HEADERS, new])
    cells = {r["updateCells"]["start"]["columnIndex"]: r["updateCells"]["rows"][0]["values"][0]["userEnteredValue"] for r in requests}
    assert cells[3] == {"stringValue": new[3]}
    assert cells[8] == {"formulaValue": new[8]}
    assert cells[0] == {"stringValue": new[0]}


def test_newest_first_moves_whole_rows_across_months_and_is_idempotent():
    before = [tracker.HEADERS, row("old", applied="31/07/2026 10:00"), row("new", applied="02/09/2026 10:00"), row("middle", applied="01/08/2026 10:00")]
    moves, after = sync.newest_first_requests(123, before)
    assert [r[3] for r in after[1:]] == ["new", "middle", "old"]
    simulated = list(before)
    for request in moves:
        move = request["moveDimension"]
        assert move["source"]["dimension"] == "ROWS"
        source, target = move["source"]["startIndex"], move["destinationIndex"]
        simulated.insert(target, simulated.pop(source))
    assert simulated == after
    assert sync.newest_first_requests(123, after)[0] == []


@pytest.fixture
def services(tmp_path, monkeypatch):
    sheets, drive = Mock(), Mock()
    sheets.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 123, "title": "Applications", "gridProperties": {"rowCount": 100}}}]}
    sheets.spreadsheets().values().get().execute.return_value = {"values": [tracker.HEADERS, row("old")]}
    monkeypatch.setattr(tracker, "google_services", Mock(return_value=(sheets, drive)))
    monkeypatch.setattr(tracker, "rows_from_db", Mock(return_value=[row("new", applied="03/09/2026 10:00")]))
    args = argparse.Namespace(google_token=tmp_path / "token.json", spreadsheet_id="test-sheet", sheet_id=123, tab_name="Applications", db_path=tmp_path / "jobs.db", repo_root=tmp_path, drive_state=tmp_path / "state/tracker_drive_files.json", drive_folder_name="Evidence", dry_run=False)
    return args, sheets, drive


def test_dry_run_never_mutates_remote_or_uploads(services):
    args, sheets, drive = services
    args.dry_run = True
    result = sync.sync_tracker(args)
    assert result["added"] == 1
    assert result["preserved_history"] == 1
    sheets.spreadsheets().batchUpdate.assert_not_called()
    sheets.spreadsheets().values().clear.assert_not_called()
    drive.files.assert_not_called()
    assert not args.drive_state.exists()


def test_sync_backs_up_then_writes_cells_colors_and_moves_in_one_batch(services):
    args, sheets, drive = services
    backup = args.drive_state.parent / "tracker_before_sync.json"
    batch = sheets.spreadsheets().batchUpdate
    def checked_batch(**kwargs):
        assert json.loads(backup.read_text())["values"][1] == row("old")
        assert backup.stat().st_mode & 0o777 == 0o600
        requests = kwargs["body"]["requests"]
        assert any("updateCells" in r for r in requests)
        assert any("repeatCell" in r for r in requests)
        assert requests[-1]["moveDimension"]["destinationIndex"] == 1
        return Mock()
    batch.side_effect = checked_batch
    result = sync.sync_tracker(args)
    assert result["rows"] == 2
    batch.assert_called_once()
    sheets.spreadsheets().values().clear.assert_not_called()


def test_concurrent_sheet_edit_aborts_before_mutation(services):
    args, sheets, drive = services
    sheets.spreadsheets().values().get().execute.side_effect = [
        {"values": [tracker.HEADERS, row("old")]},
        {"values": [tracker.HEADERS, row("edited")]},
    ]
    with pytest.raises(ValueError, match="changed during preparation"):
        sync.sync_tracker(args)
    sheets.spreadsheets().batchUpdate.assert_not_called()


def test_failed_batch_keeps_snapshot_and_does_not_mark_success(services):
    args, sheets, drive = services
    sheets.spreadsheets().batchUpdate().execute.side_effect = RuntimeError("test failure")
    with pytest.raises(RuntimeError):
        sync.sync_tracker(args)
    assert (args.drive_state.parent / "tracker_before_sync.json").exists()
    assert not (args.drive_state.parent / "tracker_sync_state.json").exists()


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        tracker.rows_from_db(path, tmp_path)
    assert not path.exists()


def test_no_change_retry_does_not_write_sheet_or_replace_snapshot(services, monkeypatch):
    args, sheets, _ = services
    rows = [tracker.HEADERS, row("old")]
    monkeypatch.setattr(tracker, "rows_from_db", Mock(return_value=rows[1:]))
    args.drive_state.parent.mkdir()
    signature = sync.hashlib.sha256(json.dumps(tracker.formatting_requests(123, rows), sort_keys=True).encode()).hexdigest()
    (args.drive_state.parent / "tracker_sync_state.json").write_text(json.dumps({"format_signature": signature}))
    backup = args.drive_state.parent / "tracker_before_sync.json"
    backup.write_text("preserved")
    result = sync.sync_tracker(args)
    assert result["changed_cells"] == result["moved_rows"] == 0
    sheets.spreadsheets().batchUpdate.assert_not_called()
    assert backup.read_text() == "preserved"


def test_confirmation_upload_replaces_form_link_and_retry_is_idempotent(services, monkeypatch):
    args, sheets, drive = services
    screenshot = args.repo_root / "submission_result.png"
    screenshot.write_bytes(b"confirmed application")
    old = row()
    old[11] = '=HYPERLINK("https://drive.google.com/form", "Open screenshot")'
    incoming = row()
    incoming[11] = str(screenshot)
    monkeypatch.setattr(tracker, "rows_from_db", Mock(return_value=[incoming]))
    sheets.spreadsheets().values().get().execute.return_value = {"values": [tracker.HEADERS, old]}
    args.drive_state.parent.mkdir()
    args.drive_state.write_text(json.dumps({"folder_id": "managed-folder", "files": {}}))
    drive.files().create().execute.return_value = {"id": "confirmation", "webViewLink": "https://drive.google.com/confirmation"}
    result = sync.sync_tracker(args)
    assert result["changed_cells"] == 1
    requests = sheets.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
    cells = [r["updateCells"] for r in requests if "updateCells" in r]
    assert cells[0]["start"]["columnIndex"] == 11
    link = cells[0]["rows"][0]["values"][0]["userEnteredValue"]["formulaValue"]
    assert link == '=HYPERLINK("https://drive.google.com/confirmation", "Open screenshot")'
    old[11] = link
    sheets.spreadsheets().values().get().execute.return_value = {"values": [tracker.HEADERS, old]}
    drive.files().create.reset_mock()
    sheets.spreadsheets().batchUpdate.reset_mock()
    assert sync.sync_tracker(args)["changed_cells"] == 0
    drive.files().create.assert_not_called()
    sheets.spreadsheets().batchUpdate.assert_not_called()


def test_reused_screenshot_path_uploads_changed_bytes_without_overwriting_old_file(tmp_path):
    screenshot = tmp_path / "submission_result.png"
    screenshot.write_bytes(b"first capture")
    state = {"folder_id": "managed-folder", "files": {}}
    drive = Mock()
    drive.files().create().execute.side_effect = [
        {"id": "one", "webViewLink": "https://drive.google.com/one"},
        {"id": "two", "webViewLink": "https://drive.google.com/two"},
    ]
    def upload():
        return tracker.upload_local_file(drive, str(screenshot), "test-job", state, tmp_path / "state.json", "Evidence", "Open screenshot", tmp_path)
    first = upload()
    assert upload() == first
    screenshot.write_bytes(b"later capture")
    second = upload()
    assert second != first
    assert upload() == second
    assert drive.files().create().execute.call_count == 2
    drive.files().update.assert_not_called()
    drive.files().delete.assert_not_called()


def test_failed_evidence_upload_keeps_existing_link_and_cache_for_retry(services, monkeypatch):
    args, sheets, drive = services
    path = args.repo_root / "submission_result.png"
    path.write_bytes(b"confirmation")
    old = row()
    old[11] = '=HYPERLINK("https://drive.google.com/form", "Open screenshot")'
    incoming = row()
    incoming[11] = str(path)
    sheets.spreadsheets().values().get().execute.return_value = {"values": [tracker.HEADERS, old]}
    monkeypatch.setattr(tracker, "rows_from_db", Mock(return_value=[incoming]))
    args.drive_state.parent.mkdir()
    state = {"folder_id": "managed-folder", "files": {str(path): {"id": "old", "webViewLink": "https://drive.google.com/form"}}}
    args.drive_state.write_text(json.dumps(state))
    drive.files().create().execute.side_effect = RuntimeError("test upload failure")
    with pytest.raises(RuntimeError, match="upload was not confirmed"):
        sync.sync_tracker(args)
    sheets.spreadsheets().batchUpdate.assert_not_called()
    assert json.loads(args.drive_state.read_text()) == state
