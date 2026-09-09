import sqlite3

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
    with sqlite3.connect(db) as conn:
        scraper.record_application_stage(conn, "job-1", "package_generated", package_path=str(package), sync=False)
    client = FakeClient()
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path)), client=client)
    monkeypatch.setattr("jobhunter_auto_apply.engine.time.sleep", lambda _: None)
    monkeypatch.setattr(engine, "inspect", lambda *args, **kwargs: None)
    engine.upload_file("job-1", "input[type=file]", str(cached), approved=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT package_path,stage FROM applications").fetchone() == (str(package), "resume_uploaded")
    assert client.uploads == [("input[type=file]", str(cached.resolve()))]
