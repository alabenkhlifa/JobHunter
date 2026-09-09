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


def test_approved_upload_preserves_permanent_package_directory(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    package = tmp_path / "output" / "job-1"
    package.mkdir(parents=True)
    cached = tmp_path / "cache" / "resume.pdf"
    cached.parent.mkdir()
    cached.write_bytes(b"test-only PDF placeholder")
    with closing(sqlite3.connect(db)) as conn, conn:
        scraper.record_application_stage(conn, "job-1", "package_generated", package_path=str(package), sync=False)
    client = FakeClient()
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path)), client=client)
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
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / "jobs.db"), tracker_sync=False, verify_submission=True), client)
    with pytest.raises(PermissionError, match="not available"):
        engine.click_submit("job-1", '[id="submit"]', approved=True)
    assert not (tmp_path / "jobs.db").exists()


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
                                        tracker_sync=False, browser_output_dir="/documents"), client)
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
