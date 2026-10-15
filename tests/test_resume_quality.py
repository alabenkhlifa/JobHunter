"""Synthetic contracts for the opt-in owner pipeline and actual PDF checks."""

import copy
from datetime import date
import json
import sqlite3
import subprocess
import sys

import fitz
import pytest

import jobhunter_interest_flow as flow
import resume_quality as quality
from resume_pdf_audit import audit_resume_pdf, resume_layout_metrics
from resume_refiner import ProfileValidationError, atomic_update_profile, project_public_resume, validate_profile
from styled_resume_pdf import StyledResumePDF


@pytest.fixture
def profile():
    bullets = [
        "Owned **3 Spring Boot services** built with **Java**, using PostgreSQL and Kubernetes on AWS.",
        "Migrated 6 production services with **zero downtime**.",
        "Onboarded and mentored 7 colleagues, including junior engineers and fellow leads.",
        "Integrated a CRM through WSDL and MQTT events.",
        "Sped up notifications by batching database updates and controlling memory use.",
        "Developed services with TDD and set up SonarQube to resolve vulnerabilities.",
    ]
    return {
        "schema_version": 2, "name": "Candidate", "headline": "Lead Software Engineer",
        "email": "candidate@example.com", "website": "https://candidate.example",
        "location": "Home, Country", "summary": "Confirmed career overview.",
        "certifications": ["AI Tool Certificate", "Scrum Certificate", "Architecture Certificate"],
        "certification_links": {"Scrum Certificate": "https://credentials.example/scrum"},
        "skills": {"Backend": ["Java", "Spring Boot", "PostgreSQL"], "Cloud": ["AWS", "Kubernetes", "UnrelatedTool"]},
        "experience": [{"id": "role-current", "title": "Lead Software Engineer", "company": "Example Employer",
                        "dates": "October 2020 - Present", "bullets": bullets},
                       {"id": "role-prior", "title": "Software Engineer", "company": "Previous Employer",
                        "dates": "August 2019 - September 2020", "bullets": ["Developed Java services."]}],
        "evidence_bank": [{"id": f"fact-{i}", "achievement_id": f"achievement-{i}", "wording": "full",
            "experience_id": "role-current", "public_text": text, "confirmation": "candidate-confirmed",
            "confidentiality": "public", "visibility": ["resume", "cover-letter"]} for i, text in enumerate(bullets)],
        "additional": {"languages": "English (C1)", "teaching": "Private legacy teaching notes"},
        "education": [{"degree": "Engineering Diploma", "school": "Example University", "dates": "2014 - 2019"}],
        "volunteering": [{"title": "University Instructor", "organization": "Teaching University",
            "dates": "09/2022 - Present", "description": "Microservices Architecture and Spring Framework",
            "confirmation": "candidate-confirmed", "confidentiality": "public",
            "match_terms": ["microservices", "spring", "mentor", "mentoring", "teaching"]}],
        "resume_quality": {"enabled": True, "preferred_pages": 1, "max_pages": 2, "hide_progression": True,
            "omittable_certifications": ["AI Tool Certificate", "Scrum Certificate", "Architecture Certificate"],
            "protected_certifications": ["Scrum Certificate"], "protected_skills": ["Java"],
            "certification_tags": {"AI Tool Certificate": ["agentic"], "Scrum Certificate": ["scrum"],
                                   "Architecture Certificate": ["architecture"]},
            "relocation_targets": {"Spain": ["Spain", "Madrid", "Barcelona"]}},
    }


@pytest.fixture
def job():
    return {"id": "quality-job", "title": "Senior Backend Engineer", "company": "Target Employer",
            "location": "Madrid, Spain", "url": "https://jobs.example/backend", "source": "example",
            "description": "Own Java Spring Boot microservices on AWS and PostgreSQL. Mentor engineers. "
                           "Build scalable integrations and improve performance with testing and Scrum.",
            "tech_required": "java, spring boot, aws, postgresql"}


def summary(context):
    return {"text": f"Lead Software Engineer with {context['career_years']}+ years of experience developing Java and Spring Boot services. "
                    "Owned 3 services using PostgreSQL and Kubernetes on AWS. "
                    "Migrated 6 services with zero downtime and mentored engineers.",
            "evidence_ids": [e["id"] for e in context["evidence"]],
            "context_sha256": context["context_sha256"], "review_flags": []}


def test_quality_is_opt_in_and_never_projects_private_teaching(profile, job):
    validate_profile(profile)
    public = project_public_resume(profile)
    assert "confirmation" not in public["volunteering"][0]
    assert "teaching" not in public["additional"]
    selected = flow._tailor_resume(profile, job)
    assert len(selected["experience"][0]["bullets"]) == 6
    assert selected["location"].endswith("Relocating to Madrid")
    assert selected["volunteering"]
    legacy = copy.deepcopy(profile)
    legacy["resume_quality"]["enabled"] = False
    assert "volunteering" not in project_public_resume(legacy)
    assert len(flow._tailor_resume(legacy, job)["experience"][0]["bullets"]) == 4


@pytest.mark.parametrize("title", ["Senior Java Engineer", "Cloud Architect", "TypeScript Backend Engineer"])
def test_reviewed_sidebar_is_role_independent_and_preserves_source_facts(profile, job, title, tmp_path):
    profile["resume_quality"]["reviewed_sidebar"] = True
    profile["volunteering"][0]["description"] = ""
    profile["volunteering"].append({
        "title": "Public AI Trainer", "organization": "", "dates": "10/2026", "description": "",
        "url": "https://candidate.example/training", "confirmation": "candidate-confirmed",
        "confidentiality": "public", "match_terms": ["backend"],
    })
    profile["education"][0].update(display_degree="Engineering Diploma", display_school="EU",
                                    location="City", dates="September 2014 - July 2019")
    original = copy.deepcopy(profile)
    validate_profile(profile)
    selected = quality.presentation(project_public_resume(profile), profile,
                                    dict(job, title=title, description="Backend services; mentor engineers."))
    assert profile == original
    assert selected["education"][0]["school"] == "Example University"
    assert selected["education"][0]["display_school"] == "EU"
    assert all("confirmation" not in item and "match_terms" not in item for item in selected["volunteering"])
    path = tmp_path / "reviewed.pdf"
    pdf = StyledResumePDF(selected); pdf.render(); pdf.output(str(path))
    assert audit_resume_pdf(path, selected)["pdf_link_count"] == 4
    with fitz.open(path) as document:
        assert len(document) == 1
        page = document[0]
        assert page.search_for("TEACHING & TRAINING")
        assert page.search_for("08/2019 - 09/2020")
        assert not page.search_for("September 2014")
        assert not page.search_for("Example University")
        assert page.search_for("EU, City")
        spans = [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]]
        school = next(s for s in spans if s["text"] == "EU, City")
        dates = next(s for s in spans if s["text"] == "09/2014 - 07/2019")
        assert school["origin"][1] == dates["origin"][1]
        trainer = next(s for s in spans if s["text"] == "Public AI Trainer")
        assert trainer["color"] == 0x090909 and trainer["size"] == pytest.approx(8.8)
        link = next(link for link in page.get_links() if link.get("uri") == "https://candidate.example/training")
        assert fitz.Rect(link["from"]).intersects(fitz.Rect(trainer["bbox"]))


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://candidate.example/training", "https://user@candidate.example/training"])
def test_teaching_links_require_safe_https(profile, url):
    profile["volunteering"][0]["url"] = url
    with pytest.raises(ProfileValidationError, match="HTTPS"):
        validate_profile(profile)


def test_teaching_link_must_match_the_confirmed_public_record(profile, job):
    profile["volunteering"][0]["url"] = "https://candidate.example/approved"
    public = project_public_resume(profile)
    public["volunteering"][0]["url"] = "https://candidate.example/unapproved"
    assert "volunteering" not in quality.presentation(public, profile, job)


def test_compact_fit_keeps_reviewed_sidebar_preferences(profile, job):
    payload = project_public_resume(profile)
    payload["layout"] = {"education_dates_inline": True, "teaching_certification_spacing": True}
    def render(value):
        assert value["layout"]["education_dates_inline"]
        assert value["layout"]["teaching_certification_spacing"]
        return 1 if value["layout"].get("compact_spacing") else 2
    fitted, _ = quality.fit_resume(payload, profile, job, render)
    assert fitted["layout"]["education_dates_inline"]


def configure_cloud_providers(profile):
    profile["resume_quality"]["cloud_provider_skills"] = ["AWS", "Azure", "OVH"]
    profile["skills"]["Cloud"].append("Azure API Management")
    profile["evidence_bank"].append(dict(profile["evidence_bank"][0], id="cloud-migration",
        public_text="Moved the production platform from AWS to OVH.", achievement_id="cloud-migration"))


@pytest.mark.parametrize("title", ["Software Architect", "Solution Architect", "Senior Cloud Engineer"])
def test_cloud_providers_lead_the_sidebar_without_mutating_approved_selection(profile, job, title):
    configure_cloud_providers(profile)
    validate_profile(profile)
    payload = project_public_resume(profile)
    payload["skills"] = {"Backend": ["Java"], "Cloud & Infrastructure": ["Azure API Management", "Terraform"],
                         "Other": ["aws", "OVH"]}
    original, source = copy.deepcopy(payload), copy.deepcopy(profile)
    selected = quality.presentation(payload, profile, dict(job, title=title))
    assert list(selected["skills"])[0] == "Cloud & Infrastructure"
    assert selected["skills"]["Cloud & Infrastructure"] == ["AWS", "Azure", "OVH", "Azure API Management", "Terraform"]
    assert "Other" not in selected["skills"]
    assert quality.presentation(selected, profile, dict(job, title=title)) == selected
    assert payload == original and profile == source


def test_cloud_provider_rule_requires_role_relevance_and_opt_in(profile, job):
    configure_cloud_providers(profile)
    payload = project_public_resume(profile)
    payload["skills"] = {"Backend": ["Java"]}
    assert quality.presentation(payload, profile, job)["skills"] == payload["skills"]
    cloud_job = dict(job, description="Own cloud infrastructure for production services.")
    assert next(iter(quality.presentation(payload, profile, cloud_job)["skills"].values()))[:3] == ["AWS", "Azure", "OVH"]
    profile["resume_quality"]["enabled"] = False
    assert quality.presentation(payload, profile, cloud_job)["skills"] == payload["skills"]


@pytest.mark.parametrize("change", ["unsupported", "private", "draft", "cover_only", "duplicate"])
def test_cloud_provider_policy_rejects_unconfirmed_or_invalid_sources(profile, change):
    configure_cloud_providers(profile)
    if change == "unsupported":
        profile["resume_quality"]["cloud_provider_skills"].append("UnconfirmedCloud")
    elif change == "duplicate":
        profile["resume_quality"]["cloud_provider_skills"].append("aws")
    elif change == "private":
        profile["evidence_bank"][-1]["confidentiality"] = "private"
    elif change == "draft":
        profile["evidence_bank"][-1]["confirmation"] = "draft"
    else:
        profile["evidence_bank"][-1]["visibility"] = ["cover-letter"]
    with pytest.raises(ProfileValidationError, match="Cloud provider"):
        validate_profile(profile)


@pytest.mark.parametrize("entries,heading", [(["Architecture decisions", "Mentoring"], "Leadership"),
                                            (["Claude", "Mentoring"], "AI-Assisted Delivery & Leadership")])
def test_skill_heading_describes_retained_entries(profile, job, entries, heading):
    payload = project_public_resume(profile)
    payload["skills"] = {"AI-Assisted Delivery & Leadership": entries}
    assert quality.presentation(payload, profile, job)["skills"] == {heading: entries}


def test_cloud_providers_survive_overflow_and_ai_heading_changes_after_trimming(profile, job):
    configure_cloud_providers(profile)
    payload = project_public_resume(profile)
    payload["skills"] = {"Cloud & Infrastructure": ["Azure API Management"],
                         "AI-Assisted Delivery & Leadership": ["Claude", "Mentoring"]}
    rendered = []
    def render(value):
        rendered.append(copy.deepcopy(value))
        return 2 if any("Claude" in v for v in value["skills"].values()) else 1
    fitted, report = quality.fit_resume(payload, profile, dict(job, title="Software Architect"), render)
    assert fitted["skills"]["Cloud & Infrastructure"][:3] == ["AWS", "Azure", "OVH"]
    assert fitted["skills"]["Leadership"] == ["Mentoring"]
    assert "AI-Assisted Delivery & Leadership" not in rendered[-1]["skills"]
    assert not {"AWS", "Azure", "OVH"} & {o["item"] for o in report["omissions"] if o["kind"] == "skill"}


def test_provider_first_row_and_accurate_heading_are_present_in_actual_pdf(profile, job, tmp_path):
    configure_cloud_providers(profile)
    payload = project_public_resume(profile)
    payload["skills"] = {"Cloud & Infrastructure": ["Azure API Management", "Terraform"],
                         "AI-Assisted Delivery & Leadership": ["Architecture decisions", "Mentoring"]}
    path = tmp_path / "providers.pdf"
    def render(value):
        pdf = StyledResumePDF(value); pdf.render(); pdf.output(str(path))
        return resume_layout_metrics(path)
    fitted, _ = quality.fit_resume(payload, profile, dict(job, title="Software Architect"), render)
    audit_resume_pdf(path, fitted)
    with fitz.open(path) as doc:
        words = doc[0].get_text("words")
        provider_words = [next(w for w in words if w[4] == name and w[0] > 350) for name in ["AWS", "Azure", "OVH"]]
        assert [w[0] for w in provider_words] == sorted(w[0] for w in provider_words)
        assert max(w[1] for w in provider_words) - min(w[1] for w in provider_words) < 1
        assert "Leadership" in doc[0].get_text() and "AI-Assisted Delivery" not in doc[0].get_text()


def configure_summary_priorities(profile):
    profile["resume_quality"]["summary_priorities"] = [
        {"role_terms": ["architect"], "focus": "Ownership and architecture of a complete production platform.",
         "evidence_ids": ["fact-0", "fact-1"]}]


def test_summary_priorities_require_selected_evidence_and_substantive_review(profile, job):
    configure_summary_priorities(profile)
    validate_profile(profile)
    job = dict(job, title="Software Architect")
    context = quality.summary_context(profile, job, project_public_resume(profile))
    assert context["summary_priorities"] == profile["resume_quality"]["summary_priorities"]
    assert context["missing_summary_evidence_ids"] == []
    draft = summary(context)
    quality.validate_summary(draft, context)
    draft["evidence_ids"].remove("fact-0")
    with pytest.raises(quality.ResumeQualityError, match="architecture priorities"):
        quality.validate_summary(draft, context)
    draft = summary(context)
    def reviewer(messages, schema):
        return draft if schema == quality.SUMMARY_SCHEMA else {"verdict": "blocked",
            "blocking_evidence_issues": ["The prose lists tasks but omits the required platform ownership."],
            "revised_draft": draft}
    with pytest.raises(quality.ResumeQualityError, match="platform ownership"):
        quality.draft_summary(context, reviewer)


def test_missing_summary_priority_fails_before_model_cannot_borrow_omitted_facts(profile, job):
    configure_summary_priorities(profile)
    payload = project_public_resume(profile)
    payload["experience"][0]["bullets"] = payload["experience"][0]["bullets"][2:]
    context = quality.summary_context(profile, dict(job, title="Software Architect"), payload)
    assert context["missing_summary_evidence_ids"] == ["fact-0", "fact-1"]
    def unexpected(*args):
        pytest.fail("A summary cannot be drafted using facts missing from the resume")
    with pytest.raises(quality.ResumeQualityError, match="fact-0"):
        quality.draft_summary(context, unexpected)
    assert "summary_priorities" not in quality.summary_context(profile, job, payload)


@pytest.mark.parametrize("change", ["private", "draft", "unknown", "empty_roles"])
def test_summary_priorities_require_confirmed_public_sources(profile, change):
    configure_summary_priorities(profile)
    if change == "private":
        profile["evidence_bank"][0]["confidentiality"] = "private"
    elif change == "draft":
        profile["evidence_bank"][0]["confirmation"] = "draft"
    elif change == "unknown":
        profile["resume_quality"]["summary_priorities"][0]["evidence_ids"] = ["unknown"]
    else:
        profile["resume_quality"]["summary_priorities"][0]["role_terms"] = []
    with pytest.raises(ProfileValidationError, match="Summary priorit"):
        validate_profile(profile)


def test_volunteering_requires_both_confirmation_and_relevance(profile, job):
    irrelevant = dict(job, description="Design embedded C systems.", tech_required="c")
    assert "volunteering" not in flow._tailor_resume(profile, irrelevant)
    profile["volunteering"][0]["confirmation"] = "draft"
    assert "volunteering" not in flow._tailor_resume(profile, job)


def test_equivalent_wording_is_deduplicated_and_draft_short_is_unusable(profile):
    full = profile["evidence_bank"][0]
    short = dict(full, id="fact-short", public_text="Owned 3 Java services.", wording="short")
    profile["evidence_bank"].append(short)
    assert quality.distinct_wording([short["public_text"], full["public_text"]], profile) == [full["public_text"]]
    short["confirmation"] = "draft"
    payload = project_public_resume(profile)
    fitted, report = quality.fit_resume(payload, profile, {}, lambda p: 2)
    assert fitted["experience"][0]["bullets"][0] == full["public_text"]
    assert not any(o["kind"] == "approved_short_wording" for o in report["omissions"])


def test_career_duration_unions_overlaps_and_requires_exact_months(profile):
    profile["experience"].append({"title": "Parallel Role", "dates": "October 2025 - Present"})
    assert quality.employment_years(profile, date(2026, 10, 10)) == 7
    profile["experience"][0]["dates"] = "2020 - Present"
    with pytest.raises(quality.ResumeQualityError, match="Month-accurate"):
        quality.employment_years(profile)


def test_summary_is_bound_to_profile_selection_and_quantities(profile, job):
    resume = flow._tailor_resume(profile, job)
    context = quality.summary_context(profile, job, resume)
    draft = summary(context)
    assert quality.validate_summary(draft, context) == draft
    for change, message in [({"context_sha256": "stale"}, "stale"),
                            ({"evidence_ids": ["private-or-missing"]}, "unavailable"),
                            ({"text": draft["text"].replace("3 services", "99 services")}, "unsupported"),
                            ({"text": draft["text"].replace("Lead Software Engineer", "Example Employer engineer")}, "prohibited")]:
        with pytest.raises(quality.ResumeQualityError, match=message):
            quality.validate_summary({**draft, **change}, context)


def test_model_draft_requires_an_independent_successful_review(profile, job):
    context = quality.summary_context(profile, job, flow._tailor_resume(profile, job))
    calls = []
    def planner(messages, schema):
        calls.append(messages)
        return summary(context) if len(calls) == 1 else {
            "verdict": "blocked", "blocking_evidence_issues": ["Unproven ownership"], "revised_draft": summary(context)}
    with pytest.raises(quality.ResumeQualityError, match="independent"):
        quality.draft_summary(context, planner)
    assert len(calls) == 2


def test_fitting_stops_without_removing_unique_items_when_it_fits(profile, job):
    payload = project_public_resume(profile)
    payload["skills"]["Cloud"].append("Java")
    original = copy.deepcopy(payload)
    fitted, report = quality.fit_resume(payload, profile, job, lambda p: 1)
    assert "UnrelatedTool" in fitted["skills"]["Cloud"]
    assert fitted["certifications"] == original["certifications"]
    assert len(report["omissions"]) == 1
    assert report["omissions"][0]["kind"] == "duplicate_skill"
    assert payload == original


def test_fit_omits_only_irrelevant_unprotected_items_and_preserves_two_pages(profile, job):
    payload = project_public_resume(profile)
    fitted, report = quality.fit_resume(payload, profile, job, lambda p: 2)
    assert fitted["certifications"] == ["Scrum Certificate"]
    assert "Java" in fitted["skills"]["Backend"]
    assert "UnrelatedTool" not in fitted["skills"]["Cloud"]
    assert report["two_page_fallback"]
    with pytest.raises(quality.ResumeQualityError, match="page limit"):
        quality.fit_resume(payload, profile, job, lambda p: 2, hard_limit=1)


def test_short_wording_precedes_omissions_and_preserves_the_fact(profile, job):
    full = profile["evidence_bank"][0]
    short = dict(full, id="approved-short", public_text="Owned **3 Spring Boot services** with Java on AWS.", wording="short")
    profile["evidence_bank"].append(short)
    def render(payload):
        return 1 if short["public_text"] in payload["experience"][0]["bullets"] else 2
    fitted, report = quality.fit_resume(project_public_resume(profile), profile, job, render)
    assert short["public_text"] in fitted["experience"][0]["bullets"]
    assert fitted["certifications"] == profile["certifications"]
    assert report["omissions"][0]["kind"] == "approved_short_wording"


def fill_projects(profile):
    profile["resume_quality"]["fill_same_employer_projects"] = True
    profile["employment_groups"] = [{
        "company": "Example Employer", "title": "Lead Software Engineer",
        "dates": "October 2020 - Present", "confirmation": "candidate-reviewed",
        "engagements": [
            {"name": "Recent Cloud Project", "dates": "August 2026 - Present", "tech": "Java, AWS",
             "bullets": ["Built Java microservices on AWS with automated tests.",
                         "Developed a Java API with PostgreSQL and reliable production delivery."]},
            {"name": "Unrelated Project", "dates": "2025 - 2026", "tech": "C#, .NET",
             "bullets": ["Developed C# services for an unrelated desktop product."]},
        ]}]
    payload = project_public_resume(profile)
    payload["experience"][0]["subtitle"] = "Original Java Project"
    return payload


def test_fill_requires_explicit_same_employer_permission_and_review(profile, job):
    payload = fill_projects(profile)
    def render(data):
        return {"pages": 1, "main_fill": 0.95 if data["experience"][0].get("engagements") else 0.5}
    before = copy.deepcopy(profile)
    profile["resume_quality"]["fill_same_employer_projects"] = False
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted == payload and not report["one_page_fill"]["additions"]
    profile["resume_quality"]["fill_same_employer_projects"] = True
    profile["employment_groups"][0]["confirmation"] = "draft"
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted == payload and not report["one_page_fill"]["additions"]
    profile["employment_groups"][0]["confirmation"] = "candidate-reviewed"
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert report["one_page_fill"]["status"] == "filled"
    projects = fitted["experience"][0]["engagements"]
    assert [p["name"] for p in projects] == ["Recent Cloud Project", "Original Java Project"]
    assert len(projects[0]["bullets"]) == 1
    assert set(projects[1]["bullets"]) == set(payload["experience"][0]["bullets"])
    assert fitted["experience"][0]["highlights"] == [
        {"context": "Original Java Project", "text": b} for b in payload["experience"][0]["bullets"][:3]]
    assert len(fitted["experience"]) == len(payload["experience"])
    assert profile == before


def test_one_page_expansion_keeps_the_best_maibornwolff_project_first(profile, job):
    payload = fill_projects(profile)
    profile["experience"][0]["company"] = "MaibornWolff GmbH"
    profile["employment_groups"][0]["company"] = "MaibornWolff GmbH"
    payload["experience"][0]["company"] = "MaibornWolff GmbH"
    profile["resume_quality"]["group_bullets_by_client"] = True
    before = copy.deepcopy(profile)
    def render(data):
        return {"pages": 1, "main_fill": 0.95 if data["experience"][0].get("engagements") else 0.5}
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert report["one_page_fill"]["additions"]
    assert [e["name"] for e in fitted["experience"][0]["engagements"]] == ["Original Java Project", "Recent Cloud Project"]
    assert profile == before


def test_rendered_maibornwolff_relevance_order_keeps_client_dates_private_without_a_caption(profile, job, tmp_path):
    payload = project_public_resume(profile)
    role = payload["experience"][0]
    role["company"] = "MaibornWolff GmbH"
    role["engagements"] = [
        {"name": "Cloud Client", "dates": "March 2025 - July 2025", "tech": "Azure",
         "bullets": ["Built Azure infrastructure."]},
        {"name": "JVM Client", "dates": "October 2020 - October 2024", "tech": "Java, Spring Boot",
         "bullets": role["bullets"][:2]},
    ]
    profile["resume_quality"]["group_bullets_by_client"] = True
    flow._prioritize_resume_bullets(payload, flow._job_relevance_text(job), job["title"], quality=True, profile=profile)
    path = tmp_path / "mw-relevance.pdf"
    pdf = StyledResumePDF(payload);pdf.render();pdf.output(str(path))
    audit_resume_pdf(path, payload, max_pages=1)
    with fitz.open(path) as document:
        text = document[0].get_text()
        assert text.index("JVM Client") < text.index("Cloud Client")
        assert "ordered by relevance" not in text
        assert "March 2025 - July 2025" not in text
        assert "October 2020 - October 2024" not in text
    assert role["dates"] == profile["experience"][0]["dates"]


def test_fill_rejects_overflow_and_restores_the_final_render(profile, job):
    payload = fill_projects(profile)
    rendered = []
    def render(data):
        rendered.append(copy.deepcopy(data))
        return {"pages": 2} if data["experience"][0].get("engagements") else {"pages": 1, "main_fill": 0.5}
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted == payload == rendered[-1]
    assert report["pages"] == 1 and not report["one_page_fill"]["additions"]
    assert any(r["experience"][0].get("engagements") for r in rendered)


def test_fill_never_adds_an_excluded_employer_or_stale_selected_client_bullets(profile, job):
    payload = fill_projects(profile)
    profile["employment_groups"][0]["engagements"][0]["name"] = "Original Java Project"
    profile["employment_groups"][0]["engagements"][1]["tech"] = "Java"
    profile["employment_groups"][0]["company"] = "Excluded Employer"
    fitted, report = quality.fit_resume(payload, profile, job, lambda p: {"pages": 1, "main_fill": 0.5})
    assert fitted == payload and not report["one_page_fill"]["additions"]
    profile["employment_groups"][0]["company"] = "Example Employer"
    profile["employment_groups"][0]["engagements"] = profile["employment_groups"][0]["engagements"][:1]
    fitted, report = quality.fit_resume(payload, profile, job, lambda p: {"pages": 1, "main_fill": 0.5})
    assert fitted == payload and not report["one_page_fill"]["additions"]


def test_fill_preserves_approved_wording_and_never_changes_font_sizes(profile, job, tmp_path):
    payload = fill_projects(profile)
    path = tmp_path / "filled.pdf"
    def render(data):
        pdf = StyledResumePDF(data)
        pdf.render()
        pdf.output(str(path))
        return resume_layout_metrics(path)
    initial = render(payload)
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert report["pages"] == 1
    assert report["one_page_fill"]["main_fill"] > initial["main_fill"]
    assert all(e["project"] == "Recent Cloud Project" for e in report["one_page_fill"]["additions"])
    assert fitted["summary"] == payload["summary"]
    assert "layout" not in fitted
    audit_resume_pdf(path, fitted, max_pages=1)
    with fitz.open(path) as document:
        text = document[0].get_text()
        assert "Unrelated Project" not in text
        assert "Built Java microservices on AWS with automated tests." in text
        fonts = [span["size"] for block in document[0].get_text("dict")["blocks"]
                 for line in block.get("lines", []) for span in line["spans"]
                 if "Built Java microservices" in span["text"]]
        assert fonts and all(abs(size - 7.7) < 0.01 for size in fonts)


def test_fill_restores_full_wording_only_when_the_measured_page_still_fits(profile, job):
    full = profile["evidence_bank"][0]
    short = dict(full, id="short", wording="short", public_text="Owned 3 Java services.")
    profile["evidence_bank"].append(short)
    payload = project_public_resume(profile)
    payload["experience"][0]["bullets"][0] = short["public_text"]
    def render(data):
        complete = full["public_text"] in data["experience"][0]["bullets"]
        return {"pages": 1, "main_fill": 0.95 if complete else 0.5}
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted["experience"][0]["bullets"][0] == full["public_text"]
    assert report["one_page_fill"]["additions"] == [{"kind": "approved_full_wording", "evidence_id": full["id"]}]
    full["confirmation"] = "draft"
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted == payload and not report["one_page_fill"]["additions"]


def test_restored_full_wording_remains_attributed_when_projects_are_added(profile, job):
    payload = fill_projects(profile)
    full = profile["evidence_bank"][0]
    short = dict(full, id="short", wording="short", public_text="Owned 3 Java services.")
    profile["evidence_bank"].append(short)
    payload["experience"][0]["bullets"][0] = short["public_text"]
    def render(data):
        role = data["experience"][0]
        if role.get("engagements"):
            for h in role.get("highlights", []):
                assert any(e["name"] == h["context"] and h["text"] in e["bullets"] for e in role["engagements"])
            return {"pages": 1, "main_fill": 0.95}
        return {"pages": 1, "main_fill": 0.7 if full["public_text"] in role["bullets"] else 0.5}
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted["experience"][0]["highlights"][0]["text"] == full["public_text"]
    assert len(report["one_page_fill"]["additions"]) == 2


def test_fill_leaves_two_pages_and_already_full_pages_unchanged(profile, job):
    payload = fill_projects(profile)
    for measured in [{"pages": 1, "main_fill": 0.96}, {"pages": 2}]:
        fitted, report = quality.fit_resume(payload, profile, job, lambda p: measured)
        assert fitted["experience"] == payload["experience"]
        assert not report["one_page_fill"]["additions"]


@pytest.mark.parametrize("field", ["fill_same_employer_projects", "group_bullets_by_client"])
def test_fill_policy_rejects_non_boolean_permission(profile, field):
    profile["resume_quality"][field] = "yes"
    with pytest.raises(ProfileValidationError, match="boolean"):
        validate_profile(profile)


def test_client_sections_keep_all_bullets_below_customer_headings(profile, job, tmp_path):
    payload = fill_projects(profile)
    profile["resume_quality"]["group_bullets_by_client"] = True
    path = tmp_path / "client-sections.pdf"
    def render(data):
        assert all("highlights" not in role for role in data["experience"])
        pdf = StyledResumePDF(data);pdf.render();pdf.output(str(path))
        return resume_layout_metrics(path)
    before = copy.deepcopy(profile)
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert report["one_page_fill"]["additions"]
    role = fitted["experience"][0]
    assert "highlights" not in role
    assert [p["name"] for p in role["engagements"]] == ["Recent Cloud Project", "Original Java Project"]
    assert set(role["engagements"][1]["bullets"]) == set(payload["experience"][0]["bullets"])
    with fitz.open(path) as document:
        page = document[0]
        for project in role["engagements"]:
            heading = page.search_for(project["name"])[0]
            for bullet in project["bullets"]:
                from resume_inline import plain_text
                lines = page.search_for(plain_text(bullet))
                assert lines and all(line.y0 > heading.y1 for line in lines)
        first_heading = page.search_for("Recent Cloud Project")[0]
        employer = page.search_for("Example Employer")[0]
        assert not any(word[4] == "•" and employer.y1 < word[1] < first_heading.y0
                       for word in page.get_text("words"))
    audit_resume_pdf(path, fitted, max_pages=1)
    assert profile == before


def test_client_sections_remove_legacy_employer_highlights_before_ranking(profile, job):
    profile["resume_quality"]["group_bullets_by_client"] = True
    payload = fill_projects(profile)
    role = payload["experience"][0]
    role["engagements"] = [{"name": role.pop("subtitle"), "bullets": role["bullets"], "dates": role["dates"]}]
    role["highlights"] = [{"context": "Original Java Project", "text": role["bullets"][0]}]
    projected = quality.presentation(payload, profile, job)
    assert "highlights" not in projected["experience"][0]
    assert "highlights" in payload["experience"][0]
    flow._prioritize_resume_bullets(payload, flow._job_relevance_text(job), job["title"], quality=True, profile=profile)
    assert "highlights" not in role


def project_openings(profile):
    payload = fill_projects(profile)
    profile["resume_quality"]["group_bullets_by_client"] = True
    opening = [
        "Architected and developed the complete Java backend for a network of customers.",
        "Designed and built separate AWS environments with Terraform and automated deployments.",
        "Trained the development team in AI-assisted development and prepared repository validation checks.",
    ]
    ids = []
    for index, text in enumerate(opening):
        evidence_id = f"project-opening-{index}"
        ids.append(evidence_id)
        profile["evidence_bank"].append({"id": evidence_id, "experience_id": "role-current", "public_text": text,
            "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"]})
    project = profile["employment_groups"][0]["engagements"][0]
    project["bullets"] = [*opening, "Prevented duplicate payments with PostgreSQL transactions and integration tests."]
    project["opening_evidence_ids"] = ids
    return payload, opening


def test_project_scope_and_cloud_opening_precede_narrow_feature_details(profile, job):
    payload, opening = project_openings(profile)
    def render(data):
        projects = data["experience"][0].get("engagements", [])
        count = sum(len(p["bullets"]) for p in projects if p["name"] == "Recent Cloud Project")
        return {"pages": 1, "main_fill": 0.95 if count >= 3 else 0.5}
    fitted, report = quality.fit_resume(payload, profile, job, render)
    project = next(p for p in fitted["experience"][0]["engagements"] if p["name"] == "Recent Cloud Project")
    assert project["bullets"] == opening
    assert "opening_evidence_ids" not in project
    assert report["one_page_fill"]["additions"][0]["bullet_count"] == 3


def test_project_is_not_added_as_features_when_its_opening_does_not_fit(profile, job):
    payload, opening = project_openings(profile)
    def render(data):
        texts = list(quality._public_bullets(data))
        return {"pages": 2} if any(b in texts for b in opening) else {"pages": 1, "main_fill": 0.5}
    fitted, report = quality.fit_resume(payload, profile, job, render)
    assert fitted == payload and not report["one_page_fill"]["additions"]


@pytest.mark.parametrize("change", ["private", "draft", "unknown", "other-project"])
def test_project_openings_require_confirmed_public_evidence_from_the_same_project(profile, change):
    _, opening = project_openings(profile)
    record = profile["evidence_bank"][-3]
    if change == "private":
        record["confidentiality"] = "private"
    elif change == "draft":
        record["confirmation"] = "draft"
    elif change == "unknown":
        profile["employment_groups"][0]["engagements"][0]["opening_evidence_ids"][0] = "missing"
    else:
        record["public_text"] = "Built a different project's infrastructure."
    with pytest.raises(ProfileValidationError, match="belonging to that project"):
        validate_profile(profile)


def test_project_opening_does_not_import_omitted_variant_bullets(profile, job):
    payload, opening = project_openings(profile)
    role = payload["experience"][0]
    role["engagements"] = [{"name": "Recent Cloud Project", "bullets": ["An existing selected Java feature."], "dates": "2026"}]
    flow._prioritize_resume_bullets(payload, job["description"], job["title"], quality=True, profile=profile)
    assert role["engagements"][0]["bullets"] == ["An existing selected Java feature."]


def test_aws_environment_work_is_recognized_without_a_backend_keyword(profile, job):
    from resume_bullet_ranking import bullet_match
    bullet = "Set up separate AWS dev and prod environments with Terraform, deployed through GitHub Actions."
    result = bullet_match(bullet, job["description"], job["title"], quality=True)
    assert "infrastructure" in result.facets
    assert "infrastructure" not in bullet_match(bullet, "Develop embedded C firmware.", quality=True).facets
    assert "infrastructure" not in bullet_match("Completed an AWS and Terraform training course.", job["description"], quality=True).facets


def test_policy_and_wording_metadata_require_confirmation(profile, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    with pytest.raises(ProfileValidationError, match="confirmation"):
        atomic_update_profile(path, {"resume_quality": {"enabled": False}}, candidate_confirmed=False)
    with pytest.raises(ProfileValidationError, match="confirmation"):
        atomic_update_profile(path, {"evidence_bank": [{"id": "fact-0", "achievement_id": "other"}]}, candidate_confirmed=False)
    profile["resume_quality"]["activation_variant_id"] = "draft-only"
    with pytest.raises(ProfileValidationError, match="activation"):
        validate_profile(profile)


def test_pdf_audit_checks_actual_bold_links_and_volunteering(profile, job, tmp_path):
    payload = flow._tailor_resume(profile, job)
    payload["summary"] = summary(quality.summary_context(profile, job, payload))["text"]
    path = tmp_path / "resume.pdf"
    pdf = StyledResumePDF(payload)
    pdf.render()
    pdf.output(str(path))
    checks = audit_resume_pdf(path, payload)
    assert checks["pdf_emphasis_verified"]
    assert checks["pdf_link_count"] == 3
    with fitz.open(path) as document:
        assert "VOLUNTEERING" in " ".join(p.get_text() for p in document)
    wrong = copy.deepcopy(payload)
    wrong["experience"][0]["bullets"].append("An achievement never rendered.")
    with pytest.raises(quality.ResumeQualityError, match="selected_text"):
        audit_resume_pdf(path, wrong)
    wrong = copy.deepcopy(payload)
    wrong["email"] = "different@example.com"
    with pytest.raises(quality.ResumeQualityError, match="link_targets"):
        audit_resume_pdf(path, wrong)


def test_malformed_emphasis_is_rejected_before_rendering(profile):
    profile["evidence_bank"][0]["public_text"] = "Unpaired **emphasis"
    with pytest.raises(ProfileValidationError, match="emphasis"):
        validate_profile(profile)


def test_quality_package_is_audited_before_promotion_and_preserved_on_failure(profile, job, tmp_path, monkeypatch):
    db, source, output = tmp_path / "jobs.db", tmp_path / "profile.json", tmp_path / "output"
    source.write_text(json.dumps(profile))
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT, source TEXT, description TEXT, tech_required TEXT)")
        keys = ("id", "title", "company", "location", "url", "source", "description", "tech_required")
        connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(job[k] for k in keys))
    monkeypatch.setattr(flow.scraper, "sync_application_tracker_if_enabled", lambda: None)
    context = quality.summary_context(profile, job, flow._tailor_resume(profile, job))
    package = flow.prepare_application_package(job["id"], db_path=db, profile_path=source, output_dir=output,
                                               resume_summary_draft=summary(context))
    manifest = json.loads(package.manifest_json.read_text())
    assert manifest["manifest_version"] == 2
    assert manifest["quality_checks"]["pdf_no_text_overlap"]
    assert list((package.package_dir / "review-pages").glob("*.png"))
    before = package.resume_pdf.read_bytes()
    import resume_pdf_audit
    monkeypatch.setattr(resume_pdf_audit, "audit_resume_pdf", lambda *a, **kw: (_ for _ in ()).throw(quality.ResumeQualityError("PDF verification failed")))
    with pytest.raises(flow.TailoringReadinessError, match="verification"):
        flow.prepare_application_package(job["id"], db_path=db, profile_path=source, output_dir=output,
                                         resume_summary_draft=summary(context))
    assert package.resume_pdf.read_bytes() == before
    assert not list(output.glob(".*.staging-*"))


def test_job_specific_choices_do_not_change_other_jobs_or_the_master(profile, job):
    profile["resume_quality"]["job_overrides"] = {job["id"]: {
        "omitted_certifications": ["AI Tool Certificate"], "protected_certifications": ["Architecture Certificate"]}}
    validate_profile(profile)
    public = project_public_resume(profile)
    fitted, report = quality.fit_resume(public, profile, job, lambda p: 1)
    assert "AI Tool Certificate" not in fitted["certifications"]
    assert report["omissions"][0]["reason"] == "Explicit candidate choice for this job"
    other, _ = quality.fit_resume(public, profile, {**job, "id": "other"}, lambda p: 1)
    assert other["certifications"] == profile["certifications"]
    overflow, _ = quality.fit_resume(public, profile, job, lambda p: 2)
    assert "Architecture Certificate" in overflow["certifications"]


@pytest.mark.parametrize("value", [None, {}, "not a list"])
def test_volunteering_type_errors_are_validation_errors(profile, value):
    profile["volunteering"] = value
    with pytest.raises(ProfileValidationError, match="list"):
        validate_profile(profile)


def test_numeric_months_preserve_exact_chronology_and_duration(profile):
    profile["experience"][0]["dates"] = "10/2020 - Present"
    profile["experience"][1]["dates"] = "08/2019 - 09/2020"
    assert quality.employment_years(profile, date(2026, 10, 10)) == 7
    assert flow._start_month("10/2020 - 10/2024") == (2020, 10)
    assert flow._end_month("10/2020 - 10/2024") == (2024, 10)


def test_draft_preview_is_reviewable_but_not_production_selectable(profile, job, tmp_path, monkeypatch):
    from resume_refiner import select_resume_variant
    public = project_public_resume(profile)
    snapshot = {k: public[k] for k in ("headline", "summary", "certifications", "certification_links", "skills", "experience", "education", "volunteering")}
    for role in snapshot["experience"]:
        role.pop("id", None)
    variant = {"id": "pending-review", "confirmation": "draft", "match_terms": ["java"], "max_pages": 2, "resume": snapshot}
    profile["resume_variants"] = [variant]
    assert select_resume_variant(profile, job["description"], job["title"]) is None
    before = copy.deepcopy(profile)
    monkeypatch.setattr(flow.scraper, "record_application_stage", lambda *a, **kw: pytest.fail("Preview touched application state"))
    def planner(messages, schema):
        request = json.loads(messages[-1]["content"])
        if schema == quality.SUMMARY_SCHEMA:
            return summary(request)
        return {"verdict": "ready", "blocking_evidence_issues": [], "revised_draft": request["draft"]}
    output = tmp_path / "preview.pdf"
    result = quality.preview_variant(profile, variant, job, output, planner=planner)
    assert result["preview_only"] and output.exists()
    assert profile == before
    assert output.with_suffix(".manifest.json").exists()
    assert list((tmp_path / "preview-pages").glob("*.png"))


def test_real_two_page_fallback_keeps_relevant_experience(profile, job, tmp_path):
    public = project_public_resume(profile)
    public["experience"][0]["bullets"] = [
        f"Implemented Java Spring Boot service feature {i} on AWS with PostgreSQL, automated tests, "
        "and reliable production delivery for customers." for i in range(40)]
    path = tmp_path / "two-pages.pdf"
    def render(payload):
        pdf = StyledResumePDF(payload)
        pdf.render()
        pdf.output(str(path))
        return pdf.page_no()
    fitted, report = quality.fit_resume(public, profile, job, render)
    assert report["pages"] == 2 and report["two_page_fallback"]
    assert fitted["experience"][0]["bullets"] == public["experience"][0]["bullets"]
    audit_resume_pdf(path, fitted)


def test_pdf_audit_rejects_fake_emphasis_clipping_and_overlap(profile, tmp_path):
    public = project_public_resume(profile)
    public["experience"][0]["bullets"] = ["Owned **3 Spring Boot services** with Java."]
    unbolded = copy.deepcopy(public)
    unbolded["experience"][0]["bullets"] = ["Owned 3 Spring Boot services with Java."]
    path = tmp_path / "bad.pdf"
    pdf = StyledResumePDF(unbolded)
    pdf.render()
    pdf.output(str(path))
    with pytest.raises(quality.ResumeQualityError, match="bold_spans"):
        audit_resume_pdf(path, public)
    with fitz.open(path) as document:
        document[0].insert_text((1, 15), "Clipped text", fontsize=8)
        document[0].insert_text((200, 750), "Overlap", fontsize=8)
        document[0].insert_text((200, 750), "Overlap", fontsize=8)
        document.save(str(tmp_path / "clipped.pdf"))
    with pytest.raises(quality.ResumeQualityError, match="margins.*text_overlap"):
        audit_resume_pdf(tmp_path / "clipped.pdf", unbolded)


def test_sidebar_heading_moves_with_first_content(profile):
    pdf = StyledResumePDF(project_public_resume(profile))
    pdf._side_schedule = {}
    pdf._plan_language_education(page=1, y=pdf.BOTTOM - 15)
    assert not pdf._side_schedule.get(1)
    assert pdf._side_schedule[2][0][2]["title"] == "Volunteering"
    assert pdf._side_schedule[2][1][0] == "volunteering"


def test_grouped_dates_are_audited_in_their_rendered_format(profile, tmp_path):
    payload = project_public_resume(profile)
    role = payload["experience"][0]
    role["engagements"] = [{"name": "Confirmed Client", "dates": "October 2020 - October 2024", "bullets": role.pop("bullets")}]
    path = tmp_path / "grouped.pdf"
    pdf = StyledResumePDF(payload)
    pdf.render()
    pdf.output(str(path))
    audit_resume_pdf(path, payload)
    role["dates"] = "October 2021 - Present"
    with pytest.raises(quality.ResumeQualityError, match="selected_text"):
        audit_resume_pdf(path, payload)


def test_preview_cli_fails_cleanly_for_a_stale_summary(profile, job, tmp_path):
    public = project_public_resume(profile)
    snapshot = {k: public[k] for k in ("headline", "summary", "skills", "experience", "education")}
    for role in snapshot["experience"]:
        role.pop("id", None)
    profile["resume_variants"] = [{"id": "review", "confirmation": "draft", "match_terms": ["java"], "resume": snapshot}]
    context = quality.summary_context(profile, job, public)
    draft = {**summary(context), "context_sha256": "stale"}
    for name, value in (("profile", profile), ("job", job), ("draft", draft)):
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    result = subprocess.run([sys.executable, quality.__file__, "--profile", str(tmp_path / "profile.json"),
        "--variant", "review", "--job-json", str(tmp_path / "job.json"), "--output", str(tmp_path / "preview.pdf"),
        "--summary-draft", str(tmp_path / "draft.json")], capture_output=True, text=True)
    assert result.returncode == 1 and "stale" in result.stderr
    assert "Traceback" not in result.stderr and not (tmp_path / "preview.pdf").exists()


def test_linear_sidebar_does_not_duplicate_personal_details(profile, tmp_path):
    payload = project_public_resume(profile)
    payload["skills"] = {f"Category {i}": [f"Verified skill {j}" for j in range(5)] for i in range(4)}
    payload["education"].append({"degree": "Earlier Engineering Degree", "school": "Previous University", "dates": "2012 - 2014"})
    path = tmp_path / "sidebar-overflow.pdf"
    pdf = StyledResumePDF(payload)
    pdf.render()
    pdf.output(str(path))
    with fitz.open(path) as document:
        all_text = " ".join(page.get_text() for page in document)
        assert all_text.count("Engineering Diploma") == 1
        assert all_text.count("Earlier Engineering Degree") == 1
        for page in document:
            assert page.get_text().count("LANGUAGES") <= 1
            assert page.get_text().count("EDUCATION") <= 1
    audit_resume_pdf(path, payload)


def test_actual_audit_rejects_duplicate_section_headings(profile, tmp_path):
    payload = project_public_resume(profile)
    path = tmp_path / "duplicate.pdf"
    pdf = StyledResumePDF(payload)
    pdf.render()
    pdf.output(str(path))
    with fitz.open(path) as document:
        document[0].insert_text((380, 750), "LANGUAGES", fontsize=10)
        document.save(str(tmp_path / "extra-heading.pdf"))
    with pytest.raises(quality.ResumeQualityError, match="duplicate_sections"):
        audit_resume_pdf(tmp_path / "extra-heading.pdf", payload)


def test_quality_variant_rejects_duplicate_credentials_with_different_dash_styles(profile):
    profile["resume_variants"] = [{"id": "review", "confirmation": "draft", "match_terms": ["java"],
        "resume": {"certifications": ["Example Certificate - Associate", "Example Certificate – Associate"]}}]
    with pytest.raises(ProfileValidationError, match="certification identity"):
        validate_profile(profile)


def test_summary_review_cannot_remove_confirmed_title_or_career_years(profile, job):
    context = quality.summary_context(profile, job, flow._tailor_resume(profile, job))
    draft = summary(context)
    for text in (draft["text"].replace(f"Lead Software Engineer with {context['career_years']}+ years of experience", "Backend engineer experienced in"),
                 draft["text"].replace("Lead Software Engineer", "Senior Software Engineer")):
        with pytest.raises(quality.ResumeQualityError, match="career opening"):
            quality.validate_summary({**draft, "text": text}, context)


def reviewed_project_variant(profile):
    from resume_refiner import _VARIANT_RESUME_FIELDS
    snapshot = {k: v for k, v in project_public_resume(profile).items() if k in _VARIANT_RESUME_FIELDS}
    for experience in snapshot["experience"]:
        experience.pop("id", None)
    role = snapshot["experience"][0]
    role["subtitle"] = "Product Platform"
    profile["employment_groups"] = [{
        "company": role["company"], "title": role["title"], "dates": role["dates"],
        "confirmation": "candidate-reviewed", "engagements": [{
            "name": role["subtitle"], "dates": role["dates"], "tech": "Java, Spring Boot, AWS",
            "bullets": list(role["bullets"]),
        }],
    }]
    variant = {"id": "reviewed", "confirmation": "candidate-confirmed", "role_terms": ["backend engineer"],
               "match_terms": ["java"], "max_pages": 2, "resume": snapshot}
    profile["resume_variants"] = [variant]
    profile["resume_quality"]["group_bullets_by_client"] = True
    return variant


def test_scope_opening_is_preserved_without_explicit_opening_metadata(profile, job):
    from resume_content_rules import audit_resume_content
    variant = reviewed_project_variant(profile)
    payload, _ = flow._resume_for_job(profile, job)
    project = payload["experience"][0]["engagements"][0]
    assert project["bullets"][0] == variant["resume"]["experience"][0]["bullets"][0]
    assert audit_resume_content(payload, profile, job)["project_openings_verified"]
    project["bullets"][0], project["bullets"][-1] = project["bullets"][-1], project["bullets"][0]
    with pytest.raises(quality.ResumeQualityError, match="scope"):
        audit_resume_content(payload, profile, job)


def test_product_delivery_precedes_audit_environment_even_if_audit_scores_higher(profile, job):
    bullets = ["Designed and built 5 integration endpoints in Azure API Management publishing events to Service Bus.",
               "Reverse-engineered Azure into Terraform and built an audit environment for load tests."]
    payload = {"experience": [{"company": "Example Employer", "engagements": [{"name": "Integration Product", "bullets": bullets}]}]}
    flow._prioritize_resume_bullets(payload, job["description"] + " Azure integrations and tests.", job["title"],
                                    quality=True, profile=profile)
    assert payload["experience"][0]["engagements"][0]["bullets"][0] == bullets[0]


def add_confirmed_payment_evidence(profile):
    text = "Built payment monitoring APIs exposing pending-payment age, webhook failures and transaction timelines."
    profile["evidence_bank"].append({"id": "payment-monitoring", "experience_id": "role-current",
        "public_text": text, "confirmation": "candidate-confirmed", "confidentiality": "public",
        "visibility": ["resume"]})
    profile["employment_groups"][0]["engagements"][0]["bullets"].append(text)
    return text


def test_missing_confirmed_domain_evidence_blocks_packages_before_model_or_state_changes(profile, job, monkeypatch):
    from resume_content_rules import variant_evidence_gaps
    variant = reviewed_project_variant(profile)
    text = add_confirmed_payment_evidence(profile)
    job = dict(job, title="Payments Backend Engineer", description=job["description"] + " Build payments APIs.")
    gaps = variant_evidence_gaps(profile, variant["resume"], job)
    assert [g["evidence_id"] for g in gaps] == ["payment-monitoring"]
    before = copy.deepcopy(profile)
    with pytest.raises(flow.TailoringReadinessError, match="payment-monitoring"):
        flow._assert_tailoring_ready(profile, job, variant)
    assert profile == before
    assert text not in variant["resume"]["experience"][0]["bullets"]
    profile["evidence_bank"][-1]["confidentiality"] = "private"
    assert not variant_evidence_gaps(profile, variant["resume"], job)


def test_independent_review_sees_unselected_public_facts_but_cannot_cite_them(profile, job):
    reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    payload, _ = flow._resume_for_job(profile, job)
    context = quality.summary_context(profile, job, payload)
    assert [e["id"] for e in context["selection_review"]["unselected_public_evidence"]] == ["payment-monitoring"]
    with pytest.raises(quality.ResumeQualityError, match="unavailable"):
        quality.validate_summary({**summary(context), "evidence_ids": ["payment-monitoring"]}, context)
    profile["evidence_bank"][-1]["confidentiality"] = "private"
    context = quality.summary_context(profile, job, payload)
    assert not context["selection_review"]["unselected_public_evidence"]


def test_critic_omission_comparison_matches_the_selected_project_update_scope(profile, job):
    from resume_content_rules import evidence_updates_for_issues
    reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    excluded = "Designed a governance workflow for a different client project."
    profile["employment_groups"][0]["engagements"].append({
        "name": "Intentionally Excluded Project", "dates": "2024 - 2025", "bullets": [excluded]})
    profile["evidence_bank"].append(dict(profile["evidence_bank"][-1], id="other-project-governance", public_text=excluded))
    payload, _ = flow._resume_for_job(profile, job)
    context = quality.summary_context(profile, job, payload)
    assert {e["id"] for e in context["selection_review"]["unselected_public_evidence"]} == {"payment-monitoring"}
    assert not evidence_updates_for_issues(profile, payload, ["Include other-project-governance."])


def test_update_preview_is_explicit_unapproved_and_preserves_profile(profile, job, tmp_path):
    variant = reviewed_project_variant(profile)
    text = add_confirmed_payment_evidence(profile)
    job = dict(job, title="Payments Backend Engineer", description=job["description"] + " Build payments APIs.")
    before = copy.deepcopy(profile)
    path = tmp_path / "proposal.pdf"
    def planner(messages, schema):
        request = json.loads(messages[-1]["content"])
        if schema == quality.SUMMARY_SCHEMA:
            return summary(request)
        return {"verdict": "ready", "blocking_evidence_issues": [], "revised_draft": request["draft"]}
    with pytest.raises(quality.ResumeQualityError, match="omits relevant"):
        quality.preview_variant(profile, variant, job, path, planner=planner)
    assert not path.exists()
    quality.preview_variant(profile, variant, job, path, planner=planner, propose_updates=True)
    payload = json.loads(path.with_suffix(".json").read_text())
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    assert text in list(quality._unique_bullets(payload))
    assert manifest["candidate_review_required"] is True
    assert manifest["proposed_evidence_updates"][0]["evidence_id"] == "payment-monitoring"
    assert not manifest["content_checks"]["evidence_gaps"]
    assert payload["location"].endswith("Relocating to Madrid")
    assert profile == before


def test_preview_cannot_force_backend_snapshot_into_an_architecture_role(profile, job, tmp_path):
    variant = reviewed_project_variant(profile)
    with pytest.raises(quality.ResumeQualityError, match="role family"):
        quality.preview_variant(profile, variant, dict(job, title="Product Architect"), tmp_path / "bad.pdf")
    assert not (tmp_path / "bad.pdf").exists()


def test_independent_critic_can_improve_a_review_proposal_with_exact_confirmed_evidence(profile, job, tmp_path):
    variant = reviewed_project_variant(profile)
    text = "Reduced backend response time with batched database updates and controlled memory use."
    profile["evidence_bank"].append({"id": "approved-performance", "experience_id": "role-current",
        "public_text": text, "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"]})
    profile["employment_groups"][0]["engagements"][0]["bullets"].append(text)
    before = copy.deepcopy(profile)
    calls = []
    def planner(messages, schema):
        request = json.loads(messages[-1]["content"])
        calls.append(schema)
        if schema == quality.SUMMARY_SCHEMA:
            return summary(request)
        if len(calls) == 2:
            return {"verdict": "blocked", "blocking_evidence_issues": ["Include approved-performance for the optimization responsibility."],
                    "revised_draft": request["draft"]}
        assert "approved-performance" in {e["id"] for e in request["context"]["evidence"]}
        return {"verdict": "ready", "blocking_evidence_issues": [], "revised_draft": request["draft"]}
    path = tmp_path / "critic-proposal.pdf"
    quality.preview_variant(profile, variant, job, path, planner=planner, propose_updates=True)
    assert len(calls) == 4
    result = json.loads(path.with_suffix(".manifest.json").read_text())
    assert result["content_review_revision_rounds"] == 1
    assert result["proposed_evidence_updates"][0]["evidence_id"] == "approved-performance"
    assert profile == before


def test_review_proposal_replaces_weaker_details_with_relevant_evidence(profile, job):
    from resume_content_rules import propose_evidence_updates, variant_evidence_gaps
    reviewed_project_variant(profile)
    text = add_confirmed_payment_evidence(profile)
    job = dict(job, title="Payments Backend Engineer", description=job["description"] + " Payments APIs.")
    payload, _ = flow._resume_for_job(profile, job)
    before = copy.deepcopy(payload)
    gaps = variant_evidence_gaps(profile, payload, job)
    proposal = propose_evidence_updates(payload, gaps, profile=profile, job=job)
    assert len(list(quality._unique_bullets(proposal))) <= len(list(quality._unique_bullets(payload)))
    assert text in list(quality._unique_bullets(proposal))
    assert payload["experience"][0]["engagements"][0]["bullets"][0] in list(quality._unique_bullets(proposal))
    assert payload == before


@pytest.mark.parametrize("field,value", [("confidentiality", "private"), ("confirmation", "draft")])
def test_critic_cannot_import_private_or_unconfirmed_evidence(profile, job, field, value):
    from resume_content_rules import evidence_updates_for_issues
    reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    profile["evidence_bank"][-1][field] = value
    payload, _ = flow._resume_for_job(profile, job)
    assert not evidence_updates_for_issues(profile, payload, ["Add payment-monitoring and invented-id."])


def test_content_review_retries_are_bounded_and_never_publish_a_rejected_pdf(profile, job, tmp_path, monkeypatch):
    variant = reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    monkeypatch.setattr(quality, "MAX_CONTENT_REVIEW_ROUNDS", 1)
    calls = []
    def planner(messages, schema):
        request = json.loads(messages[-1]["content"]); calls.append(schema)
        if schema == quality.SUMMARY_SCHEMA:
            return summary(request)
        return {"verdict": "blocked", "blocking_evidence_issues": ["Include payment-monitoring."],
                "revised_draft": request["draft"]}
    with pytest.raises(quality.ResumeQualityError, match="independent"):
        quality.preview_variant(profile, variant, job, tmp_path / "rejected.pdf", planner=planner, propose_updates=True)
    assert len(calls) == 2 and not (tmp_path / "rejected.pdf").exists()


@pytest.mark.parametrize("suffix", [": required evidence.", ".", "; include this."])
def test_critic_evidence_ids_allow_sentence_punctuation_without_matching_longer_ids(profile, job, suffix):
    from resume_content_rules import evidence_updates_for_issues
    reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    payload, _ = flow._resume_for_job(profile, job)
    assert [e["evidence_id"] for e in evidence_updates_for_issues(profile, payload, ["Include payment-monitoring" + suffix])] == ["payment-monitoring"]
    for longer in ("payment-monitoring.other", "payment-monitoring:other", "payment-monitoring-other"):
        assert not evidence_updates_for_issues(profile, payload, ["Include " + longer])
