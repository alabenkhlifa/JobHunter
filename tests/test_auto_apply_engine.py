from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest
import scraper

from jobhunter_auto_apply.engine import (
    ApplyConfig,
    AutoApplyEngine,
    PageInspection,
    inspect_page,
    inspection_to_markdown,
)
from jobhunter_auto_apply.cdp import CDPError, CDPTarget, connect_first_page
from jobhunter_auto_apply.cli import build_parser


class FakeClient:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.uploads = []
        self.clicked = []

    def evaluate(self, expression, **kwargs):
        if "document.querySelector" in expression and ".click" in expression:
            self.clicked.append(expression)
            return {"ok": True}
        return self.payload

    def upload_file(self, selector, file_path):
        self.uploads.append((selector, file_path))

    def screenshot(self, path):
        return path


def test_inspect_page_detects_sensitive_questions_and_blockers():
    client = FakeClient(
        {
            "url": "https://example.test/apply",
            "title": "Apply",
            "text": "Please solve captcha. Expected salary? Do you need visa sponsorship?",
            "inputs": [{"label": "Expected salary", "required": True}],
            "buttons": [],
            "links": [],
        }
    )

    inspection = inspect_page(client)

    assert inspection.url == "https://example.test/apply"
    assert "captcha" in inspection.blockers
    assert any("Expected salary" in q for q in inspection.sensitive_questions)
    assert not inspection.safe_to_continue


def test_inspect_page_requires_loaded_page_and_complete_fields():
    blank = inspect_page(FakeClient({"url": "about:blank", "title": "", "text": ""}))
    assert not blank.loaded_application_page
    assert not blank.safe_to_continue

    incomplete = inspect_page(FakeClient({
        "url": "https://jobs.example.test/apply",
        "title": "Apply",
        "text": "Application",
        "inputs": [{"label": "Current salary", "required": True, "value_present": False, "invalid": True}],
        "challenge_visible": True,
    }))
    assert incomplete.missing_required == ["Current salary"]
    assert incomplete.invalid_fields == ["Current salary"]
    assert "human verification" in incomplete.blockers
    assert not incomplete.safe_to_continue


def test_engine_does_not_record_blank_inspection(tmp_path):
    db = tmp_path / "jobs.db"
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), evidence_enabled=False),
                             client=FakeClient({"url": "about:blank", "title": "", "text": ""}))
    with pytest.raises(CDPError, match="blank or still loading"):
        engine.inspect("job-1")
    assert not db.exists()


def test_cdp_connection_requires_unambiguous_application_tab(monkeypatch):
    targets = [
        CDPTarget("blank", "", "about:blank", "page", "ws://localhost/blank"),
        CDPTarget("one", "One", "https://jobs.example.test/one", "page", "ws://localhost/one"),
        CDPTarget("two", "Two", "https://jobs.example.test/two", "page", "ws://localhost/two"),
    ]
    monkeypatch.setattr("jobhunter_auto_apply.cdp.list_targets", lambda *args: targets)
    selected = Mock()
    monkeypatch.setattr("jobhunter_auto_apply.cdp.CDPClient", lambda url, timeout: selected if url.endswith("/two") else None)
    with pytest.raises(CDPError, match="multiple application tabs"):
        connect_first_page()
    assert connect_first_page(expected_url="https://jobs.example.test/two") is selected
    with pytest.raises(CDPError, match="application page not found"):
        connect_first_page(expected_url="https://jobs.example.test/other")


def test_mutating_cli_commands_require_exact_page_url():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["upload", "--job-id", "job-1", "--selector", "input[type=file]", "--file", "resume.pdf", "--approved"])
    with pytest.raises(SystemExit):
        parser.parse_args(["submit", "--job-id", "job-1", "--selector", "button[type=submit]", "--approved"])


def test_markdown_summary_includes_required_fields():
    inspection = PageInspection(
        url="https://example.test/apply",
        title="Apply",
        text_excerpt="",
        inputs=[{"label": "Phone", "required": True}],
    )

    md = inspection_to_markdown(inspection)

    assert "Application page inspection" in md
    assert "Phone" in md
    assert "Safe to continue" in md


def test_upload_requires_approval(tmp_path):
    db = tmp_path / "jobs.db"
    file_path = tmp_path / "resume.pdf"
    file_path.write_bytes(b"pdf")
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path)), client=FakeClient())

    with pytest.raises(PermissionError):
        engine.upload_file("job-1", "input[type=file]", str(file_path), approved=False)


def test_submit_requires_approval(tmp_path):
    db = tmp_path / "jobs.db"
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path)), client=FakeClient())

    with pytest.raises(PermissionError):
        engine.click_submit("job-1", "button[type=submit]", approved=False)


def test_mutations_require_exact_application_url(tmp_path):
    document = tmp_path / "resume.pdf"
    document.write_bytes(b"pdf")
    client = Mock()
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db")), client)
    with pytest.raises(PermissionError, match="page URL is required"):
        engine.upload_file("job-1", "input[type=file]", str(document), approved=True)
    with pytest.raises(PermissionError, match="page URL is required"):
        engine.click_submit("job-1", "button[type=submit]", approved=True)
    client.upload_file.assert_not_called()
    client.evaluate.assert_not_called()


def test_approved_upload_preserves_permanent_package_directory(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    package = tmp_path / "output" / "job-1"
    package.mkdir(parents=True)
    cached = tmp_path / "cache" / "resume.pdf"
    cached.parent.mkdir()
    cached.write_bytes(b"test-only PDF placeholder")
    with closing(sqlite3.connect(db)) as conn, conn:
        scraper.record_application_stage(conn, "job-1", "package_generated", package_path=str(package), sync=False)
    client = FakeClient("https://example.test/apply")
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path),
                                        expected_page_url="https://example.test/apply"), client=client)
    monkeypatch.setattr("jobhunter_auto_apply.engine.time.sleep", lambda _: None)
    monkeypatch.setattr(engine, "inspect", lambda *args, **kwargs: None)
    engine.upload_file("job-1", "input[type=file]", str(cached), approved=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        assert conn.execute("SELECT package_path,stage FROM applications").fetchone() == (str(package), "resume_uploaded")
    assert client.uploads == [("input[type=file]", str(cached.resolve()))]


def test_scoped_submit_records_attempt_and_skips_global_tracker(tmp_path, monkeypatch):
    client = FakeClient()
    tracker = Mock()
    monkeypatch.setattr(scraper, "sync_application_tracker_if_enabled", tracker)
    monkeypatch.setattr("jobhunter_auto_apply.engine.time.sleep", lambda _: None)
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), output_dir=str(tmp_path),
                                        tracker_sync=False, verify_submission=True,
                                        expected_page_url="https://example.test/apply"), client)
    monkeypatch.setattr(engine, "inspect", Mock())
    engine.click_submit("job-1", '[id="submit"]', approved=True)
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as db, db:
        assert db.execute("SELECT stage FROM applications").fetchone()[0] == "submission_attempted"
    tracker.assert_not_called()
    assert "location.href" in client.clicked[0]


def test_scoped_missing_submit_control_never_records_success(tmp_path, monkeypatch):
    client = Mock()
    client.evaluate.return_value = {"ok": False}
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), tracker_sync=False, verify_submission=True,
                                        expected_page_url="https://example.test/apply"), client)
    with pytest.raises(PermissionError, match="not submitted"):
        engine.click_submit("job-1", '[id="submit"]', approved=True)
    assert not (tmp_path / "jobs.db").exists()


def test_submit_is_only_an_attempt_until_receipt_is_verified(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("jobhunter_auto_apply.engine.time.sleep", lambda _: None)
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), tracker_sync=False,
                                        expected_page_url="https://example.test/apply"), client)
    monkeypatch.setattr(engine, "inspect", Mock())
    engine.click_submit("job-1", "button[type=submit]", approved=True)
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as db:
        assert db.execute("SELECT stage FROM applications").fetchone()[0] == "submission_attempted"


def test_scoped_upload_checks_url_at_mutation_time(tmp_path):
    document = tmp_path / "Resume.pdf"
    document.write_bytes(b"pdf")
    client = Mock()
    client.evaluate.return_value = "https://example.test/different"
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), tracker_sync=False,
                                        expected_page_url="https://example.test/approved"), client)
    with pytest.raises(PermissionError, match="changed"):
        engine.upload_file("job-1", '[id="resume"]', str(document), approved=True)
    client.upload_file.assert_not_called()


def test_container_upload_uses_readonly_mount_path_after_host_validation(tmp_path, monkeypatch):
    output = tmp_path / "candidate/output"
    document = output / "job package" / "Resume.pdf"
    document.parent.mkdir(parents=True)
    document.write_bytes(b"%PDF-1.7\nprivate candidate resume")
    document.chmod(0o600)
    client = Mock()
    client.evaluate.return_value = "https://example.test/approved"
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), output_dir=str(output),
                                        tracker_sync=False, browser_output_dir="/documents",
                                        expected_page_url="https://example.test/approved"), client)
    monkeypatch.setattr("jobhunter_auto_apply.engine.time.sleep", lambda _: None)
    monkeypatch.setattr(engine, "inspect", Mock())
    engine.upload_file("job-1", '[id="resume"]', str(document), approved=True)
    client.upload_file.assert_called_once_with('[id="resume"]', "/documents/job package/Resume.pdf")
    assert document.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("symlink", [False, True])
def test_container_upload_rejects_other_candidate_file_or_symlink(tmp_path, symlink):
    output = tmp_path / "candidate/output"
    output.mkdir(parents=True)
    foreign = tmp_path / "other-candidate.pdf"
    foreign.write_bytes(b"private other candidate")
    document = foreign
    if symlink:
        document = output / "Resume.pdf"
        document.symlink_to(foreign)
    client = Mock()
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), output_dir=str(output),
                                        tracker_sync=False, browser_output_dir="/documents",
                                        expected_page_url="https://example.test/apply"), client)
    with pytest.raises(PermissionError, match="outside"):
        engine.upload_file("job-1", '[id="resume"]', str(document), approved=True)
    client.upload_file.assert_not_called()
    assert not (tmp_path / "jobs.db").exists()


def test_container_upload_still_requires_explicit_confirmation(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    document = output / "Resume.pdf"
    document.write_bytes(b"pdf")
    client = Mock()
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), output_dir=str(output),
                                        tracker_sync=False, browser_output_dir="/documents"), client)
    with pytest.raises(PermissionError, match="explicit approval"):
        engine.upload_file("job-1", '[id="resume"]', str(document), approved=False)
    client.upload_file.assert_not_called()
