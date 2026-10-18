import json

import fitz
import pytest

import jobhunter_interest_flow as flow
from test_resume_pipeline import manifest_of, store
from test_resume_evidence import java_job, profile as legacy_profile


@pytest.fixture
def block_profile(legacy_profile):
    legacy_profile["resume_blocks"] = {
        "version": 1,
        "enabled": True,
        "base": {"name": legacy_profile["name"], "location": legacy_profile["location"],
                 "additional": legacy_profile["additional"], "skills": {"Backend": ["Java", "Spring Boot"]}},
        "flavors": [{"id": "software-engineer", "label": "Software Engineer",
                     "confirmation": "candidate-confirmed", "headline": "Software Engineer | Java",
                     "summary": "Approved summary kept exactly."}],
        "projects": [{"id": "cars", "label": "Cars Platform", "confirmation": "candidate-confirmed",
                      "company": "Group Co", "role": {"title": "Lead Engineer", "dates": "October 2020 - Present"},
                      "entry": {"name": "Cars Platform", "dates": "October 2020 - October 2024",
                                "role": "Senior Engineer", "tech": "Java, Spring Boot",
                                "bullets": ["Owned and maintained 5 Spring Boot microservices in Kotlin on AWS.",
                                            "Migrated 8 production services to Spring Boot 3 with zero downtime."]}}],
        "variants": [{"id": "software-engineer-core", "label": "Core projects", "flavor_id": "software-engineer",
                      "confirmation": "candidate-confirmed", "max_pages": 2,
                      "project_ids": ["cars"], "default_project_ids": ["cars"]}],
    }
    return legacy_profile


def test_fixed_block_package_bypasses_model_and_records_selection_and_layout(block_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, block_profile,
                  java_job(location="Madrid, Spain", url="https://x", source="s"))

    def forbidden_planner(*_args, **_kwargs):
        pytest.fail("fixed blocks must not call the summary planner")

    selection = {"flavor_id": "software-engineer", "variant_id": "software-engineer-core", "project_ids": ["cars"]}
    package = flow.prepare_application_package("j1", resume_selection=selection, summary_planner=forbidden_planner, **paths)
    manifest = manifest_of(package)
    resume = json.loads(package.resume_json.read_text())
    assert manifest["tailoring_mode"] == "fixed_blocks"
    assert manifest["resume_selection"] == selection
    assert len(manifest["resume_blocks_sha256"]) == 64
    assert manifest["summary_review"]["source"] == "fixed"
    assert manifest["layout"]["target_pages"] == 2
    assert 1 <= manifest["resume_pages"] <= 2
    assert resume["headline"] == "Software Engineer | Java"
    assert resume["summary"] == "Approved summary kept exactly."
    assert resume["experience"][0]["engagements"][0]["bullets"] == block_profile["resume_blocks"]["projects"][0]["entry"]["bullets"]
    with fitz.open(package.resume_pdf) as document:
        assert len(document) == manifest["resume_pages"]
        assert "Approved summary kept exactly." in document[0].get_text()


def test_fixed_block_package_requires_explicit_selection(block_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, block_profile,
                  java_job(location="Madrid, Spain", url="https://x", source="s"))
    with pytest.raises(flow.TailoringReadinessError, match="Choose a confirmed resume flavor"):
        flow.prepare_application_package("j1", **paths)
