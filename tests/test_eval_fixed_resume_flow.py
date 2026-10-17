"""The fixed resume evaluator uses a private SQLite backup and fake transport."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import eval_fixed_resume_flow as evaluator
import scraper


def _profile():
    base = {
        "name": "Test Candidate", "location": "Tunis, Tunisia",
        "additional": {"languages": "English (C1)"},
        "skills": {"Backend": ["Java", "Spring Boot"]},
        "education": [], "certifications": [], "certification_links": {},
        "volunteering": [], "layout": {},
    }
    projects = []
    for project_id, label, bullet in (
        ("project-one", "Payments API", "Built the payments API with Java and Spring Boot."),
        ("project-two", "Event Platform", "Designed the event platform with PostgreSQL and AWS."),
    ):
        projects.append({
            "id": project_id, "label": label, "confirmation": "candidate-confirmed",
            "company": "Example Employer",
            "role": {"title": "Senior Engineer", "dates": "2020 - Present"},
            "entry": {"name": label, "dates": "2021 - 2023", "role": "Backend Engineer",
                      "tech": "Java, Spring Boot", "bullets": [bullet]},
        })
    return {
        **base,
        "resume_blocks": {
            "version": 1, "enabled": True, "base": base,
            "flavors": [{"id": "software-engineer", "label": "Software Engineer",
                         "confirmation": "candidate-confirmed", "headline": "Senior Software Engineer",
                         "summary": "A fixed approved summary."}],
            "projects": projects,
            "variants": [
                {"id": "software-engineer-one-page", "label": "1 page", "flavor_id": "software-engineer",
                 "confirmation": "candidate-confirmed", "max_pages": 1,
                 "project_ids": [item["id"] for item in projects],
                 "default_project_ids": [item["id"] for item in projects]},
                {"id": "software-engineer-two-pages", "label": "Up to 2 pages", "flavor_id": "software-engineer",
                 "confirmation": "candidate-confirmed", "max_pages": 2,
                 "project_ids": [item["id"] for item in projects],
                 "default_project_ids": [item["id"] for item in projects]},
            ],
        },
    }


def _inputs(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(_profile()), encoding="utf-8")
    profile_path.chmod(0o600)
    source_db = tmp_path / "source.db"
    monkeypatch.setitem(scraper.CONFIG, "db_path", source_db)
    connection = scraper.init_db()
    for number in range(1, 4):
        connection.execute(
            "INSERT INTO jobs (id,title,company,location,url,source,description,tech_required,status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"job-{number}", f"Senior Backend Engineer {number}", f"Example Co {number}",
             "Madrid, Spain", f"https://example.com/jobs/{number}", "fixture",
             "Build Java Spring Boot backend services with PostgreSQL.", "Java, Spring Boot", "new"),
        )
    connection.commit()
    connection.close()
    return source_db, profile_path


def test_flow_uses_deterministic_private_backup_and_never_submits(tmp_path, monkeypatch):
    source_db, profile_path = _inputs(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-disabled")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-disabled")
    first_report_path = tmp_path / "first.json"
    second_report_path = tmp_path / "second.json"

    first = evaluator.run_evaluation(source_db, profile_path, first_report_path,
                                     sample_size=1, seed=42, research_live=False)
    second = evaluator.run_evaluation(source_db, profile_path, second_report_path,
                                      sample_size=1, seed=42, research_live=False)

    assert first["evaluated_count"] == 1
    assert first["source_db_opened_read_only"] is True
    assert first["sqlite_backup_api_used"] is True
    assert first["all_stopped_before_submission"] is True
    assert first["results"][0]["job_id"] == second["results"][0]["job_id"]
    result = first["results"][0]
    assert result["no_generation_before_selection"] is True
    assert result["duplicate_generate_token_rejected"] is True
    assert result["fixed_summary_and_bullets_exact"] is True
    assert result["pdf_audit"]["pdf_text_verified"] is True
    assert result["final_stage"] == "approved_to_prepare_apply"
    assert result["submitted"] is False
    assert first_report_path.stat().st_mode & 0o777 == 0o600

    connection = sqlite3.connect(source_db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE status <> 'new'").fetchone()[0] == 0
    finally:
        connection.close()


def test_source_db_is_rejected_when_only_ineligible_jobs_exist(tmp_path, monkeypatch):
    source_db, profile_path = _inputs(tmp_path, monkeypatch)
    connection = sqlite3.connect(source_db)
    connection.execute("UPDATE jobs SET title=''")
    connection.commit()
    connection.close()
    with pytest.raises(evaluator.EvaluationError, match="No eligible unsubmitted"):
        evaluator.run_evaluation(source_db, profile_path, tmp_path / "none.json", sample_size=1)
