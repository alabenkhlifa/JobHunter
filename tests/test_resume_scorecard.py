"""The rubric in code: a package gets a score and flags instead of a refusal."""

import resume_evidence as evidence
import resume_scorecard as scorecard
from test_resume_evidence import blueprint, java_job, profile  # noqa: F401


def strong_payload():
    return {"headline": "Senior Backend Engineer | Java & Kotlin",
            "summary": "Lead Engineer with 7+ years of experience. Owned Spring Boot microservices on AWS. "
                       "Migrated services with zero downtime.",
            "skills": {"Backend": ["Java", "Kotlin", "Spring Boot", "AWS", "Kubernetes"]},
            "experience": [{"title": "Lead Engineer", "company": "Group Co", "engagements": [
                {"name": "Cars Platform", "bullets": [
                    "Owned and maintained 5 Spring Boot microservices in Kotlin on AWS.",
                    "Migrated 8 production services to Spring Boot 3 with zero downtime.",
                    "Onboarded and mentored 7 colleagues."]}]}]}


def test_strong_relevant_one_page_package_scores_high(profile, blueprint):
    needs = evidence.posting_needs(java_job(), evidence.catalogue(profile))
    report = scorecard.score_package(strong_payload(), profile, needs, blueprint,
                                     layout={"pages": 1, "main_fills": [0.93], "dropped": [], "dropped_projects": []},
                                     summary={"source": "model", "review_verdict": "ready", "flags": []},
                                     audit={"pdf_text_verified": True})
    assert report["score"] >= 80 and report["needs_review"] is False
    assert report["dimensions"]["role_family_fit"] == 5 and report["dimensions"]["page_use"] == 5
    assert report["dimensions"]["relevance"] >= 4 and report["dimensions"]["bullet_strength"] >= 4


def test_wrong_family_weak_bullets_and_thin_page_two_are_flagged(profile, blueprint):
    needs = evidence.posting_needs({"title": "Software Architect", "description": "Architect platforms on Azure.",
                                    "tech_required": "azure, terraform"}, evidence.catalogue(profile))
    payload = strong_payload()
    payload["experience"][0]["engagements"][0]["bullets"] = ["Did some other confirmed work.", "Helped with tasks."]
    report = scorecard.score_package(payload, profile, needs, dict(blueprint, pages=2),
                                     layout={"pages": 2, "main_fills": [1.0, 0.12], "dropped": [], "dropped_projects": []},
                                     summary={"source": "fallback", "review_verdict": None, "flags": []},
                                     audit={"pdf_text_verified": True})
    assert report["needs_review"] is True and report["score"] < 60
    assert report["dimensions"]["role_family_fit"] <= 2 and report["dimensions"]["page_use"] <= 2
    assert report["dimensions"]["bullet_strength"] <= 2 and report["dimensions"]["relevance"] <= 2
    assert any("family" in flag for flag in report["flags"]) and any("page two" in flag for flag in report["flags"])
    assert any("fallback" in flag for flag in report["flags"])


def test_repeated_figures_lower_the_repetition_dimension(profile, blueprint):
    needs = evidence.posting_needs(java_job(), evidence.catalogue(profile))
    payload = strong_payload()
    payload["summary"] = ("Lead Engineer with 7+ years of experience. Owned 5 Spring Boot microservices and migrated 8 "
                          "services with zero downtime. Mentored 7 colleagues across 5 teams and 8 projects.")
    payload["experience"][0]["engagements"][0]["bullets"].append("Served 15,000 users with 15,000 daily sessions.")
    payload["experience"][0]["engagements"][0]["bullets"].append("Scaled to 15,000 users on Kubernetes.")
    report = scorecard.score_package(payload, profile, needs, blueprint,
                                     layout={"pages": 1, "main_fills": [0.9], "dropped": [], "dropped_projects": []},
                                     summary={"source": "model", "review_verdict": "ready", "flags": []},
                                     audit={"pdf_text_verified": True})
    assert report["dimensions"]["repetition"] <= 3
    assert any("15,000" in flag for flag in report["flags"])


def test_overflow_and_audit_failure_are_critical(profile, blueprint):
    needs = evidence.posting_needs(java_job(), evidence.catalogue(profile))
    report = scorecard.score_package(strong_payload(), profile, needs, blueprint,
                                     layout={"pages": 3, "main_fills": [1, 1, 0.2], "dropped": [], "dropped_projects": [],
                                             "note": "overflow"},
                                     summary={"source": "model", "review_verdict": "blocked", "flags": ["x"]},
                                     audit={"pdf_text_verified": True})
    assert report["needs_review"] is True and report["dimensions"]["page_use"] == 1
    assert any("blocked" in flag for flag in report["flags"])
