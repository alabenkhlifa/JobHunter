from unittest.mock import Mock

import pytest

from jobhunter_integrations.google_tracker import ensure_drive_folder, share_drive_folder


def test_sharing_grants_only_named_readers_without_notifications():
    drive = Mock()
    drive.permissions().list().execute.return_value = {"permissions": []}
    assert share_drive_folder(drive, "managed-folder", "Reader@Example.com, reader@example.com") == ["reader@example.com"]
    drive.permissions().create.assert_called_once_with(
        fileId="managed-folder", body={"type": "user", "role": "reader", "emailAddress": "reader@example.com"},
        sendNotificationEmail=False, fields="id",
    )


def test_existing_writer_access_is_not_downgraded_or_duplicated():
    drive = Mock()
    drive.permissions().list().execute.side_effect = [
        {"permissions": [], "nextPageToken": "next"},
        {"permissions": [{"type": "user", "role": "writer", "emailAddress": "reader@example.com"}]},
    ]
    share_drive_folder(drive, "managed-folder", "reader@example.com")
    drive.permissions().create.assert_not_called()
    assert drive.permissions().list.call_args.kwargs["pageToken"] == "next"


def test_invalid_recipient_fails_before_google_call():
    drive = Mock()
    with pytest.raises(ValueError, match="valid tracker sharing"):
        share_drive_folder(drive, "managed-folder", "not-an-email")
    drive.permissions.assert_not_called()


def test_existing_folder_sharing_is_checked_even_without_new_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TRACKER_SHARE_WITH", "reader@example.com")
    drive = Mock()
    drive.permissions().list().execute.return_value = {"permissions": []}
    state = {"folder_id": "managed-folder"}
    assert ensure_drive_folder(drive, state, tmp_path / "uploads.json", "Evidence") == "managed-folder"
    drive.files.assert_not_called()
    assert state["shared_with"] == ["reader@example.com"]


def test_failed_share_is_not_recorded_as_success(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TRACKER_SHARE_WITH", "reader@example.com")
    drive = Mock()
    drive.permissions().list().execute.return_value = {"permissions": []}
    drive.permissions().create().execute.side_effect = RuntimeError("test failure")
    state = {"folder_id": "managed-folder"}
    with pytest.raises(RuntimeError):
        ensure_drive_folder(drive, state, tmp_path / "uploads.json", "Evidence")
    assert "shared_with" not in state
