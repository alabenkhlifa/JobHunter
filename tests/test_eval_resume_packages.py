"""Deterministic checks of the resume evaluation harness (tools/eval_resume_packages.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import eval_resume_packages as harness  # noqa: E402


@pytest.mark.parametrize(("title", "family", "level"), [
    ("Java Microservices Architect", "architect", "architect"),
    ("Product Architect", "architect", "architect"),
    ("Lead Software Engineer", "lead", "lead"),
    ("Technical Lead Java (Remote)", "lead", "lead"),
    ("Senior Backend Engineer (Java & Kafka)", "backend", "senior"),
    ("Senior Software Engineer - Java FullStack - Platform Services", "platform", "senior"),
    ("Lead Software Engineer – Cloud & API Architecture", "architect", "lead"),
])
def test_posting_family_and_seniority(title, family, level):
    assert harness.role_family(title) == family
    assert harness.seniority(title) == level


@pytest.mark.parametrize(("message", "expected"), [
    ("No candidate-confirmed resume variant matches this architecture role.", "architect_role_gate"),
    ("Resume variant omits relevant confirmed evidence: ev-x.", "evidence_gap"),
    ("Resume selection omits candidate-required summary evidence: ev-y", "summary_priorities"),
    ("Summary did not pass the independent evidence review: weak", "summary_review_blocked"),
    ("Summary requires three to four sentences", "summary_validation"),
    ("Relevant approved content exceeds the permitted page limit", "page_limit"),
    ("PDF verification failed: margins", "pdf_audit"),
    ("Generated resume is 2 pages; the confirmed variant allows at most 1", "page_limit"),
    ("Something else entirely", "other"),
])
def test_refusal_classes(message, expected):
    assert harness.refusal_class(message) == expected


def test_repetition_report_finds_exact_near_and_figure_repeats():
    bullets = [
        "Owned **5 Spring Boot microservices** on AWS with Kubernetes.",
        "Owned 5 Spring Boot microservices on AWS with Kubernetes.",
        "Migrated 8 production services from Spring Boot 2 to 3 with zero downtime.",
        "Migrated 8 production services and the shared Spring library to Spring Boot 3 with zero downtime.",
        "Mentored 7 colleagues.",
    ]
    report = harness.repetition_report(bullets)
    assert report["exact_duplicates"] == ["Owned 5 Spring Boot microservices on AWS with Kubernetes."]
    assert len(report["near_duplicates"]) == 1
    assert report["repeated_figures"] == []
    report = harness.repetition_report(["Serves 15,000 users daily.", "Scaled to 15,000 users."])
    assert report["repeated_figures"] == ["15,000"]


def test_truth_check_accepts_only_confirmed_wording_and_titles():
    profile = {
        "experience": [{"id": "r", "title": "Engineer", "company": "Co", "dates": "2020 - Present",
                        "bullets": ["Built the thing."]}],
        "evidence_bank": [{"id": "e", "experience_id": "r", "public_text": "Owned **the service**.",
                           "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"]},
                          {"id": "d", "experience_id": "r", "public_text": "Draft only.",
                           "confirmation": "draft", "confidentiality": "public", "visibility": ["resume"]}],
    }
    resume = {"experience": [{"title": "Engineer", "company": "Co",
                              "engagements": [{"name": "P", "bullets": ["Owned the service.", "Built the thing."]}]}]}
    assert harness.truth_violations(resume, profile) == []
    resume["experience"][0]["engagements"][0]["bullets"].append("Draft only.")
    resume["experience"].append({"title": "Architect", "company": "Co", "bullets": ["Built the thing."]})
    problems = harness.truth_violations(resume, profile)
    assert any(p.startswith("bullet not confirmed: Draft only.") for p in problems)
    assert "title not confirmed: Co / Architect" in problems


def test_composite_is_zero_for_hard_failures_and_scales_judge_scores():
    refused = {"status": "refused"}
    assert harness.composite(refused) == 0
    package = {"status": "package", "truth_violations": [], "audit_passed": True,
               "judge": {name: 4 for name in harness.RUBRIC}}
    assert harness.composite(package) == 80
    assert harness.composite({**package, "truth_violations": ["x"]}) == 0
    assert harness.composite({**package, "audit_passed": False}) == 0
    fallback = {"status": "package", "truth_violations": [], "audit_passed": True, "family_match": True,
                "repetition": {"exact_duplicates": [], "near_duplicates": [], "repeated_figures": []},
                "page_fill": [{"main_fill": 0.9}]}
    assert harness.composite(fallback) == 100
