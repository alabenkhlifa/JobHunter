import json
import re
from unittest.mock import Mock

import pytest

from jobhunter_integrations import google_tracker_setup as setup
from jobhunter_integrations.google_tracker import HEADERS


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    sheets, drive = Mock(), Mock()
    files, headers, shares = {}, [], {}

    def create(**kwargs):
        body = kwargs["body"]
        file_id = f"resource-{len(files)}"
        files[file_id] = {"id": file_id, **body, "owners": [{"emailAddress": "jobs@example.com"}],
                          "capabilities": {"canShare": True}}
        return Mock(execute=lambda: {"id": file_id})

    def find(**kwargs):
        marker = re.search("value='([^']+)'", kwargs["q"])[1]
        return Mock(execute=lambda: {"files": [{"id": key} for key, value in files.items()
                                               if value.get("appProperties", {}).get("jobhunter_provision") == marker]})

    def permissions(**kwargs):
        return Mock(execute=lambda: {"permissions": [
            {"type": "user", "role": "reader", "emailAddress": email}
            for email in shares.get(kwargs["fileId"], [])]})

    def share(**kwargs):
        def execute():
            shares.setdefault(kwargs["fileId"], []).append(kwargs["body"]["emailAddress"])
            return {"id": "permission"}
        return Mock(execute=execute)

    def update(**kwargs):
        def execute():
            for request in kwargs["body"]["requests"]:
                if "updateCells" in request:
                    headers[:] = [v["userEnteredValue"]["stringValue"] for v in request["updateCells"]["rows"][0]["values"]]
            return {}
        return Mock(execute=execute)

    drive.files().create.side_effect = create
    drive.files().list.side_effect = find
    drive.files().get.side_effect = lambda **kwargs: Mock(execute=lambda: files[kwargs["fileId"]])
    drive.permissions().list.side_effect = permissions
    drive.permissions().create.side_effect = share
    sheets.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 0, "title": "Applications"}}]}
    sheets.spreadsheets().values().get.side_effect = lambda **kwargs: Mock(execute=lambda: {"values": [list(headers)]} if headers else {})
    sheets.spreadsheets().batchUpdate.side_effect = update
    monkeypatch.setattr(setup, "checked_services", Mock(return_value=(sheets, drive)))
    return sheets, drive, files, headers, shares


def provision(tmp_path, **kwargs):
    return setup.provision_tracker(tmp_path / "secrets/tracker.json", "jobs@example.com", tmp_path / "state/setup.json", **kwargs)


def test_create_formats_14_columns_and_shares_both_resources_once(cloud, tmp_path, monkeypatch):
    sheets, drive, files, headers, shares = cloud
    monkeypatch.setenv("JOBHUNTER_TRACKER_SHARE_WITH", "owner@example.com")
    kwargs = {"viewer_email": "Personal@Example.com", "drive_state_path": tmp_path / "state/uploads.json"}
    result = provision(tmp_path, **kwargs)
    assert headers == HEADERS and len(headers) == 14
    assert len(files) == 2
    assert shares == {result["spreadsheet_id"]: ["personal@example.com"], result["drive_folder_id"]: ["personal@example.com"]}
    assert result["shared_with"] == ["personal@example.com"]
    upload_state = json.loads(kwargs["drive_state_path"].read_text())
    assert upload_state["account"] == "jobs@example.com"
    assert upload_state["share_with"] == ["personal@example.com"]
    assert provision(tmp_path, **kwargs) == result
    assert drive.files().create.call_count == 2
    assert drive.permissions().create.call_count == 2
    assert sheets.spreadsheets().batchUpdate.call_count == 1
    setup.checked_services.assert_called_with(tmp_path / "secrets/tracker.json", "jobs@example.com")


def test_no_viewer_does_not_grant_personal_mailbox_access_or_inherit_owner_sharing(cloud, tmp_path, monkeypatch):
    _, drive, _, _, shares = cloud
    monkeypatch.setenv("JOBHUNTER_TRACKER_SHARE_WITH", "owner@example.com")
    result = provision(tmp_path)
    assert result["shared_with"] == [] and shares == {}
    drive.permissions().create.assert_not_called()


def test_connect_existing_keeps_all_cells_and_formatting(cloud, tmp_path):
    sheets, _, files, headers, _ = cloud
    headers[:] = HEADERS
    files["existing-sheet"] = {"id": "existing-sheet", "mimeType": setup.SHEET_MIME,
                               "owners": [{"emailAddress": "jobs@example.com"}], "capabilities": {"canShare": True}}
    result = provision(tmp_path, spreadsheet_id="existing-sheet", viewer_email="personal@example.com")
    assert result["spreadsheet_id"] == "existing-sheet"
    sheets.spreadsheets().batchUpdate.assert_not_called()
    sheets.spreadsheets().values().update.assert_not_called()
    sheets.spreadsheets().values().clear.assert_not_called()
    assert len(files) == 2


def test_other_account_owned_sheet_is_rejected_before_writes(cloud, tmp_path):
    sheets, drive, files, _, _ = cloud
    files["other-sheet"] = {"id": "other-sheet", "mimeType": setup.SHEET_MIME,
                            "owners": [{"emailAddress": "owner@example.com"}], "capabilities": {"canShare": True}}
    with pytest.raises(setup.GmailAuthError, match="owned"):
        provision(tmp_path, spreadsheet_id="other-sheet", viewer_email="personal@example.com")
    drive.files().create.assert_not_called()
    drive.permissions().create.assert_not_called()
    sheets.spreadsheets().batchUpdate.assert_not_called()


def test_reusing_setup_state_for_other_account_is_rejected_without_google(cloud, tmp_path):
    provision(tmp_path)
    setup.checked_services.reset_mock()
    with pytest.raises(setup.GmailAuthError, match="different account"):
        setup.provision_tracker(tmp_path / "secrets/other.json", "other@example.com", tmp_path / "state/setup.json")
    setup.checked_services.assert_not_called()


def test_lost_create_response_is_recovered_without_duplicate(cloud, tmp_path):
    _, drive, files, _, _ = cloud
    original = drive.files().create.side_effect

    def lost(**kwargs):
        original(**kwargs)
        raise RuntimeError("private-provider-payload")

    drive.files().create.side_effect = lost
    with pytest.raises(setup.GmailAuthError) as error:
        provision(tmp_path)
    assert "private-provider-payload" not in str(error.value)
    assert len(files) == 1
    drive.files().create.side_effect = original
    result = provision(tmp_path)
    assert len(files) == 2 and result["spreadsheet_id"] == "resource-0"
    assert drive.files().create.call_count == 2


def test_unconfirmed_create_is_not_blindly_retried(cloud, tmp_path):
    _, drive, _, _, _ = cloud
    drive.files().create.side_effect = RuntimeError("private payload")
    with pytest.raises(setup.GmailAuthError):
        provision(tmp_path)
    with pytest.raises(setup.GmailAuthError, match="uncertain result"):
        provision(tmp_path)
    assert drive.files().create.call_count == 1


def test_partial_share_retries_only_missing_grant(cloud, tmp_path):
    _, drive, _, _, shares = cloud
    original = drive.permissions().create.side_effect

    def fail_folder(**kwargs):
        if kwargs["fileId"] == "resource-1":
            raise RuntimeError("private payload")
        return original(**kwargs)

    drive.permissions().create.side_effect = fail_folder
    with pytest.raises(setup.GmailAuthError):
        provision(tmp_path, viewer_email="personal@example.com")
    assert shares == {"resource-0": ["personal@example.com"]}
    drive.permissions().create.side_effect = original
    provision(tmp_path, viewer_email="personal@example.com")
    assert shares == {"resource-0": ["personal@example.com"], "resource-1": ["personal@example.com"]}
    assert drive.files().create.call_count == 2


def test_invalid_viewer_fails_before_google_or_local_state(cloud, tmp_path):
    with pytest.raises(setup.GmailAuthError):
        provision(tmp_path, viewer_email="not-email")
    setup.checked_services.assert_not_called()
    assert not (tmp_path / "state").exists()
