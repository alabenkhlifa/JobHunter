"""Tests for importing a reviewed PDF into the fixed resume catalog."""

import copy
import json
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import import_resume_blocks as importer


def _payload():
    return {
        "name": "Test Candidate", "email": "test@example.com", "phone": "+1 555 0100",
        "website": "https://example.com", "linkedin": "linkedin.com/in/test",
        "location": "City", "skills": {"Backend": ["Python"]}, "certifications": [],
        "certification_links": {}, "education": [], "additional": {"languages": ["English"]},
        "volunteering": [], "layout": {}, "headline": "Senior Software Engineer",
        "summary": "A confirmed public summary.",
        "experience": [{
            "title": "Senior Engineer", "company": "Example Co", "dates": "2020 – Present",
            "location": "Remote", "bullets": [],
            "engagements": [{"name": "Example Platform", "dates": "2021 – 2023",
                             "role": "Lead Engineer", "tech": "Python", "bullets": ["Built a platform."]}],
        }],
    }


def _write_inputs(tmp_path):
    payload = _payload()
    payload_path = tmp_path / "reviewed.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    reference = tmp_path / "reference.pdf"
    importer._render(payload, reference)
    profile_path = tmp_path / "profile.json"
    profile = {"schema_version": 2, "private_marker": {"keep": True}, "experience": [{"private": "retained"}]}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    return profile_path, payload_path, reference, profile


def test_dry_run_checks_render_without_mutating_profile(tmp_path):
    profile_path, payload_path, reference, profile = _write_inputs(tmp_path)
    before = profile_path.read_bytes()

    assert importer.import_catalog(profile_path, payload_path, reference) is True
    assert profile_path.read_bytes() == before
    assert not profile_path.with_name("profile.json.bak").exists()


def test_apply_preserves_profile_and_creates_private_backup(tmp_path):
    profile_path, payload_path, reference, original = _write_inputs(tmp_path)

    assert importer.import_catalog(profile_path, payload_path, reference, apply=True) is True
    updated = json.loads(profile_path.read_text())
    assert {key: updated[key] for key in original} == original
    assert updated["resume_blocks"]["metadata"]["source_pdf_sha256"]
    assert (profile_path.with_name("profile.json.bak")).read_bytes() == json.dumps(original).encode()
    assert profile_path.stat().st_mode & 0o777 == 0o600


def test_same_import_is_idempotent_and_different_catalog_refused(tmp_path):
    profile_path, payload_path, reference, _ = _write_inputs(tmp_path)
    importer.import_catalog(profile_path, payload_path, reference, apply=True)
    assert importer.import_catalog(profile_path, payload_path, reference, apply=True) is False

    changed = _payload()
    changed["summary"] = "A different reviewed summary."
    changed_path = tmp_path / "other.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    importer._render(changed, reference)
    with pytest.raises(importer.ImportErrorSafe, match="different fixed catalog"):
        importer.import_catalog(profile_path, changed_path, reference, apply=True)


def test_rejects_reference_pdf_that_does_not_match_payload(tmp_path):
    profile_path, payload_path, reference, _ = _write_inputs(tmp_path)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 40), "unrelated PDF")
    doc.save(reference)
    doc.close()
    with pytest.raises(importer.ImportErrorSafe, match="does not match"):
        importer.import_catalog(profile_path, payload_path, reference, apply=True)
    assert not profile_path.with_name("profile.json.bak").exists()
