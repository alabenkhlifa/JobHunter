"""The blueprint pipeline inside prepare_application_package: scored packages instead of refusals."""

import copy
import json
import sqlite3

import fitz
import pytest

import jobhunter_interest_flow as flow
import resume_pipeline as pipeline
import resume_quality as quality
from resume_inline import normalized_text
from resume_pdf_audit import resume_layout_metrics
from styled_resume_pdf import StyledResumePDF
from test_resume_blueprints import full_profile  # noqa: F401
from test_resume_evidence import blueprint, item, java_job, profile  # noqa: F401


def architect_job():
    return {"id": "arch-1", "title": "Java Microservices Architect", "company": "Target", "location": "Madrid, Spain",
            "url": "https://jobs.example/arch", "source": "example",
            "description": "Architect Java Spring Boot microservices on AWS with Kubernetes and Terraform. "
                           "Own the platform end to end and mentor engineers.",
            "tech_required": "java, spring boot, aws, kubernetes, terraform"}


def store(tmp_path, monkeypatch, profile, job):
    db, source, output = tmp_path / "jobs.db", tmp_path / "profile.json", tmp_path / "output"
    source.write_text(json.dumps(profile))
    keys = ("id", "title", "company", "location", "url", "source", "description", "tech_required")
    with sqlite3.connect(db) as connection:
        connection.execute(f"CREATE TABLE jobs ({', '.join(k + ' TEXT' for k in keys)})")
        connection.execute(f"INSERT INTO jobs VALUES ({', '.join('?' * len(keys))})", tuple(job[k] for k in keys))
    monkeypatch.setattr(flow.scraper, "sync_application_tracker_if_enabled", lambda: None)
    return {"db_path": db, "profile_path": source, "output_dir": output}


def summary_from(context):
    cited = [e["id"] for e in context["evidence"]][:3]
    return {"text": f"{context['career_opening']}. Owned backend services and their infrastructure. "
                    "Delivered production migrations with zero downtime.",
            "evidence_ids": cited, "context_sha256": context["context_sha256"], "review_flags": []}


def planner_with(reviews=None, *, fail=False, flags=None):
    calls = []

    def planner(messages, schema):
        if fail:
            raise RuntimeError("model unavailable")
        request = json.loads(messages[-1]["content"])
        calls.append((schema is quality.SUMMARY_SCHEMA, request))
        if schema is quality.SUMMARY_SCHEMA:
            draft = summary_from(request)
            if flags is not None:
                draft["review_flags"] = flags
            return draft
        issues = reviews.pop(0) if reviews else []
        return {"verdict": "blocked" if issues else "ready", "blocking_evidence_issues": issues,
                "revised_draft": request["draft"]}
    planner.calls = calls
    return planner


def manifest_of(package):
    return json.loads(package.manifest_json.read_text())


def bullets_of(package):
    resume = json.loads(package.resume_json.read_text())
    return [b for r in resume["experience"] for e in (r.get("engagements") or [r]) for b in e["bullets"]]


def test_architect_posting_gets_a_scored_blueprint_package(full_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, full_profile, architect_job())
    package = flow.prepare_application_package("arch-1", summary_planner=planner_with(), **paths)
    manifest = manifest_of(package)
    assert manifest["manifest_version"] == 3 and manifest["tailoring_mode"] == "blueprint"
    assert manifest["selected_blueprint_id"] == "architect" and manifest["selected_variant_id"] is None
    assert isinstance(manifest["resume_score"], int) and isinstance(manifest["resume_needs_review"], bool)
    assert manifest["scorecard"]["dimensions"]["role_family_fit"] == 5
    assert manifest["layout"]["target_pages"] == 2 and 1 <= manifest["resume_pages"] <= 2
    assert manifest["quality_checks"]["pdf_text_verified"] and manifest["quality_checks"]["role_variant_required"] is False
    assert manifest["evidence_proposal_applied"] is False
    assert manifest["summary_review"]["source"] == "model" and manifest["summary_review"]["review_verdict"] == "ready"
    resume = json.loads(package.resume_json.read_text())
    assert resume["summary"].startswith("Software architect with ") and resume["headline"] == "Software Architect | Cloud & Backend"
    confirmed = {normalized_text(e["public_text"]) for e in full_profile["evidence_bank"]}
    assert bullets_of(package) and all(normalized_text(b) in confirmed for b in bullets_of(package))
    farm = next(e for r in resume["experience"] for e in r.get("engagements") or [] if e["name"] == "Farm Network")
    assert farm["role"] == "Software Architect"
    with fitz.open(package.resume_pdf) as document:
        assert "Software Architect" in document[0].get_text()
    with sqlite3.connect(paths["db_path"]) as conn:
        assert conn.execute("SELECT stage FROM applications").fetchall() == [("package_generated",)]


def test_model_failure_falls_back_to_the_blueprint_summary_with_a_flag(full_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, full_profile, java_job(location="Madrid, Spain", url="https://x", source="s"))
    package = flow.prepare_application_package("j1", summary_planner=planner_with(fail=True), **paths)
    manifest = manifest_of(package)
    assert manifest["summary_review"]["source"] == "fallback"
    assert json.loads(package.resume_json.read_text())["summary"] == "Fallback summary."
    assert any(flag.startswith("summary: fallback") for flag in manifest["score_flags"])


def test_blocked_review_falls_back_to_the_confirmed_summary_and_records_the_issues(full_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, full_profile, java_job(location="Madrid, Spain", url="https://x", source="s"))
    planner = planner_with([["Claim not supported: zero downtime"]])
    package = flow.prepare_application_package("j1", summary_planner=planner, **paths)
    manifest = manifest_of(package)
    assert manifest["summary_review"]["source"] == "fallback" and manifest["summary_review"]["review_verdict"] == "blocked"
    assert manifest["summary_review"]["flags"] == ["Claim not supported: zero downtime"]
    assert json.loads(package.resume_json.read_text())["summary"] == "Fallback summary."
    assert manifest["scorecard"]["dimensions"]["summary_quality"] == 3
    assert len(planner.calls) == 2  # one draft, one review; no retry loop


def test_summary_opens_with_the_headline_title_and_tells_one_named_project_story(full_profile, tmp_path, monkeypatch):
    backend = full_profile["resume_blueprints"][0]
    backend["summary"] = {"default": "Fallback summary.", "senior": "Senior backend engineer approved wording."}
    backend["summary_stack"] = "Java, Kotlin and Spring Boot"
    job = java_job(location="Madrid, Spain", url="https://x", source="s")
    plan_ = pipeline.plan(full_profile, job)
    context = pipeline.summary_context(full_profile, job, plan_)
    assert context["career_opening"] == f"Senior backend engineer with {context['career_years']}+ years in Java, Kotlin and Spring Boot"
    assert context["style_example"] == "Senior backend engineer approved wording."
    assert "selection_review" not in context
    assert [p["name"] for p in context["projects"]] == [p.name for p in plan_.selection.projects]
    assert {i for p in context["projects"] for i in p["evidence_ids"]} == {e["id"] for e in context["evidence"]
                                                                          if not e["id"].startswith("volunteering:")}
    project = context["projects"][0]
    draft = {"text": f"{context['career_opening']}. For {project['name']}, owned backend services and delivered "
                     "production migrations with zero downtime.",
             "evidence_ids": project["evidence_ids"][:2], "context_sha256": context["context_sha256"], "review_flags": []}
    text, info = pipeline.summarize(full_profile, job, plan_, draft=draft)
    assert text == draft["text"] and info["source"] == "provided_reviewed_draft"
    four = dict(draft, text=draft["text"] + " Mentored engineers. Ran on-call.")
    with pytest.raises(quality.ResumeQualityError, match="2 to 3 sentences"):
        pipeline.summarize(full_profile, job, plan_, draft=four)
    first_person = dict(draft, text=draft["text"].replace("owned backend", "I've owned backend"))
    with pytest.raises(quality.ResumeQualityError, match="first-person"):
        pipeline.summarize(full_profile, job, plan_, draft=first_person)
    assert "forbidden_names" not in context
    lead = dict(draft, text=draft["text"].replace("Senior backend engineer", "Lead Engineer"))
    with pytest.raises(quality.ResumeQualityError, match="career opening"):
        pipeline.summarize(full_profile, job, plan_, draft=lead)
    assert pipeline.summarize(full_profile, job, plan_, planner=planner_with(fail=True))[0] == "Senior backend engineer approved wording."


def test_a_previous_blueprint_summary_record_can_be_reused_for_a_repackage(full_profile, tmp_path, monkeypatch):
    job = java_job(location="Madrid, Spain", url="https://x", source="s")
    plan_ = pipeline.plan(full_profile, job)
    text, info = pipeline.summarize(full_profile, job, plan_, planner=planner_with())
    record = {"text": text, **info}
    assert pipeline.summarize(full_profile, job, plan_, draft=record)[0] == text
    fallback, info = pipeline.summarize(full_profile, job, plan_, planner=planner_with([["blocked"]]))
    reused, again = pipeline.summarize(full_profile, job, plan_, draft={"text": fallback, **info})
    assert reused == fallback == "Fallback summary." and again["source"] == "fallback"


def test_oversized_review_flags_are_trimmed_not_refused(full_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, full_profile, java_job(location="Madrid, Spain", url="https://x", source="s"))
    planner = planner_with(flags=[f"flag {i} " + "x" * 400 for i in range(14)])
    package = flow.prepare_application_package("j1", summary_planner=planner, **paths)
    review = manifest_of(package)["summary_review"]
    assert review["source"] == "model" and len(review["flags"]) == 10 and all(len(f) <= 300 for f in review["flags"])


def test_proposal_arguments_stay_accepted_for_the_autopilot(full_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, full_profile, java_job(location="Madrid, Spain", url="https://x", source="s"))
    package = flow.prepare_application_package("j1", summary_planner=planner_with(), accept_evidence_proposals=True,
                                               prior_evidence_ids=["cars-tdd"], **paths)
    manifest = manifest_of(package)
    assert manifest["evidence_proposal_applied"] is False and manifest["proposed_evidence_updates"] == []
    assert manifest["selection"]["family"] == "backend"
    assert all(set(p) >= {"name", "company", "evidence_ids"} for p in manifest["selection"]["projects"])


def test_supplied_summary_draft_must_match_the_current_selection(full_profile, tmp_path, monkeypatch):
    paths = store(tmp_path, monkeypatch, full_profile, java_job(location="Madrid, Spain", url="https://x", source="s"))
    stale = {"text": "Lead Engineer with 7+ years of experience. Owned services. Shipped migrations.",
             "evidence_ids": ["cars-own"], "context_sha256": "0" * 64, "review_flags": []}
    with pytest.raises(flow.TailoringReadinessError, match="stale"):
        flow.prepare_application_package("j1", resume_summary_draft=stale, **paths)


def test_profiles_without_blueprints_keep_the_legacy_path(profile, tmp_path, monkeypatch):
    assert not pipeline.enabled(profile)
    paths = store(tmp_path, monkeypatch, profile, architect_job())
    with pytest.raises(flow.TailoringReadinessError, match="Software Architect resume variant"):
        flow.prepare_application_package("arch-1", render_pdfs=False, **paths)


def test_cover_letter_context_uses_the_blueprint_resume(full_profile, tmp_path, monkeypatch):
    context = flow.cover_letter_draft_context(full_profile, architect_job())
    assert context["public_evidence"] and all(e["quote"] for e in context["public_evidence"])


def test_layout_metrics_report_every_page_and_renderer_shows_project_roles(tmp_path):
    payload = {"name": "Candidate", "headline": "H", "summary": "S", "additional": {"languages": "English (C1)"},
               "experience": [{"title": "Lead", "company": "Co", "dates": "2020 - Present", "engagements": [
                   {"name": "Project " + str(i), "dates": "2021 - 2022", "role": "Software Architect" if i == 0 else "",
                    "bullets": ["Owned the service and its deployment pipeline on AWS with Kubernetes."] * 6}
                   for i in range(8)]}]}
    pdf = StyledResumePDF(payload)
    pdf.render()
    path = tmp_path / "r.pdf"
    pdf.output(str(path))
    metrics = resume_layout_metrics(path)
    assert metrics["pages"] == 2 and len(metrics["main_fills"]) == 2 and 0 < metrics["main_fills"][1] <= 1
    with fitz.open(path) as document:
        assert "Project 0 · Software Architect" in document[0].get_text().replace("\n", " ")


def test_irrelevant_skills_keep_each_category_head_and_posting_or_bullet_terms():
    profile = {"resume_quality": {"protected_skills": ["Kubernetes"]}}
    job = {"title": "Lead Software Engineer", "description": "Lead a Java team. CI/CD with GitHub Actions.", "tech_required": "java"}
    payload = {"skills": {"Backend": ["Java", "Kotlin", "Spring Boot", "MQTT", "Kubernetes", "Docker", "GitHub Actions"],
                          "Leadership": ["Team management", "Mentoring", "Agile/Scrum", "Cross-functional coordination"]},
               "experience": [{"company": "Group Co", "engagements": [{"name": "Cars", "bullets": ["Ran services on Docker."]}]}]}
    assert pipeline._irrelevant_skills(profile, job, payload) == [
        ("Backend", "MQTT"), ("Leadership", "Cross-functional coordination")]
