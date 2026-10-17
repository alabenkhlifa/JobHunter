"""Role-family blueprints: validation, choice per posting and the renderer payload they build."""

import copy

import pytest

import resume_blueprints as blueprints
import resume_evidence as evidence
from test_resume_evidence import blueprint, item, java_job, profile  # noqa: F401


@pytest.fixture
def full_profile(profile, blueprint):
    profile.update({
        "email": "c@example.com", "phone": "+1 555", "website": "https://candidate.example", "linkedin": "linkedin.com/in/c",
        "certifications": ["AWS Certificate", "Scrum Certificate"],
        "certification_links": {"AWS Certificate": "https://credly.example/aws"},
        "education": [{"degree": "Engineering Diploma", "school": "Example University", "dates": "09/2014 - 07/2019"}],
        "volunteering": [{"id": "v1", "title": "Instructor", "organization": "University", "dates": "2022 - Present",
                          "description": "", "confirmation": "candidate-confirmed", "confidentiality": "public",
                          "match_terms": ["spring", "java"]}],
        "resume_quality": {"enabled": True, "hide_progression": True, "reviewed_sidebar": True},
    })
    profile["experience"][1]["company_url"] = "https://startup.example"
    profile["employment_groups"][0]["progression"] = "Promoted twice."
    architect = dict(copy.deepcopy(blueprint), id="architect", families=["architect"], pages=2, show_project_roles=True,
                     headline={"default": "Software Architect | Cloud & Backend"},
                     lead_kinds=["architecture", "ownership", "infrastructure"],
                     optional_companies={"Startup": "always", "Old Co": "if_relevant"},
                     projects={"max": 6, "bullets": {"lead": 4, "other": 3}, "pinned": []},
                     certifications=["Scrum Certificate", "AWS Certificate", "Unknown Certificate"])
    backend = dict(copy.deepcopy(blueprint), headline={"default": "Lead Engineer | Java & Kotlin",
                                                       "senior": "Senior Backend Engineer | Java & Kotlin"},
                   certifications=["AWS Certificate"], default=True)
    profile["resume_blueprints"] = [backend, architect,
                                    dict(copy.deepcopy(blueprint), id="draft", families=["lead"], confirmation="draft")]
    return profile


def test_blueprints_are_validated(full_profile):
    assert blueprints.enabled(full_profile)
    assert not blueprints.enabled({"name": "x"})
    bad = copy.deepcopy(full_profile)
    bad["resume_blueprints"][0]["pages"] = 3
    with pytest.raises(blueprints.BlueprintError, match="pages"):
        blueprints.validate(bad)
    bad = copy.deepcopy(full_profile)
    bad["resume_blueprints"][0]["lead_kinds"] = ["magic"]
    with pytest.raises(blueprints.BlueprintError, match="kind"):
        blueprints.validate(bad)
    bad = copy.deepcopy(full_profile)
    bad["resume_blueprints"][0]["optional_companies"] = {"Startup": "sometimes"}
    with pytest.raises(blueprints.BlueprintError, match="optional_companies"):
        blueprints.validate(bad)
    bad = copy.deepcopy(full_profile)
    bad["resume_blueprints"][1]["id"] = "backend"
    with pytest.raises(blueprints.BlueprintError, match="duplicate"):
        blueprints.validate(bad)
    bad = copy.deepcopy(full_profile)
    del bad["resume_blueprints"][0]["headline"]
    with pytest.raises(blueprints.BlueprintError, match="headline"):
        blueprints.validate(bad)
    bad = copy.deepcopy(full_profile)
    bad["resume_blueprints"][0]["summary"] = {"senior": "Only a senior summary."}
    with pytest.raises(blueprints.BlueprintError, match="summary"):
        blueprints.validate(bad)
    bad = copy.deepcopy(full_profile)
    bad["resume_blueprints"][0]["summary_stack"] = " "
    with pytest.raises(blueprints.BlueprintError, match="summary_stack"):
        blueprints.validate(bad)


def test_career_opening_and_fallback_follow_the_headline(full_profile):
    backend, architect = full_profile["resume_blueprints"][:2]
    senior, lead = evidence.posting_needs({"title": "Senior Backend Engineer"}), evidence.posting_needs({"title": "Backend Engineer"})
    assert blueprints.career_opening(backend, senior, 7) == "Senior backend engineer with 7+ years in Java and Kotlin"
    assert blueprints.career_opening(backend, lead, 7) == "Lead engineer with 7+ years in Java and Kotlin"
    assert blueprints.career_opening(architect, senior, 7) == "Software architect with 7+ years in Cloud and Backend"
    backend = dict(backend, summary_stack="Java, Kotlin and Spring Boot",
                   headline={"default": "Lead AWS Engineer"}, summary={"default": "Lead.", "senior": "Senior."})
    assert blueprints.career_opening(backend, senior, 7) == "Lead AWS engineer with 7+ years in Java, Kotlin and Spring Boot"
    assert blueprints.summary_for(backend, senior) == "Senior." and blueprints.summary_for(backend, lead) == "Lead."


def test_choose_by_role_family_with_default_fallback_and_no_drafts(full_profile):
    assert blueprints.choose(full_profile, {"title": "Software Architect"})["id"] == "architect"
    assert blueprints.choose(full_profile, {"title": "Java Microservices Architect"})["id"] == "architect"
    assert blueprints.choose(full_profile, {"title": "Senior Platform Engineer"})["id"] == "backend"
    assert blueprints.choose(full_profile, {"title": "Lead Software Engineer"})["id"] == "backend"  # draft lead is unusable
    assert blueprints.choose(full_profile, {"title": "Data Scientist"})["id"] == "backend"
    full_profile["resume_blueprints"][0].pop("default")
    assert blueprints.choose(full_profile, {"title": "Data Scientist"}) is None


def test_build_uses_only_confirmed_wording_and_profile_identity(full_profile):
    job = java_job()
    chosen = blueprints.choose(full_profile, job)
    selection = evidence.select(full_profile, job, chosen)
    payload = blueprints.build(full_profile, job, chosen, selection)
    confirmed = {e["public_text"] for e in full_profile["evidence_bank"]}
    for role in payload["experience"]:
        for entry in role.get("engagements") or [role]:
            assert entry["bullets"] and all(b in confirmed for b in entry["bullets"])
    assert payload["name"] == "Candidate" and payload["email"] == "c@example.com"
    assert payload["headline"] == "Senior Backend Engineer | Java & Kotlin"
    assert payload["summary"] == "Fallback summary."
    assert payload["skills"] == {"Backend": ["Java", "Kotlin"]}
    assert payload["certifications"] == ["AWS Certificate"]
    assert payload["certification_links"] == {"AWS Certificate": "https://credly.example/aws"}
    assert payload["additional"]["languages"] == "English (C1)"
    assert payload["education"][0]["school"] == "Example University"
    assert "resume_blueprints" not in payload and "evidence_bank" not in payload


def test_build_groups_the_main_employer_and_orders_employers(full_profile):
    job = java_job(title="Lead Software Engineer", description="Lead a team of engineers on Java Spring Boot services.")
    chosen = blueprints.choose(full_profile, job)
    selection = evidence.select(full_profile, job, dict(chosen, projects={"max": 6, "bullets": {"lead": 4, "other": 3}}))
    payload = blueprints.build(full_profile, job, chosen, selection)
    companies = [r["company"] for r in payload["experience"]]
    assert companies == ["Group Co", "Startup", "Old Co"]
    group = payload["experience"][0]
    assert group["title"] == "Lead Engineer" and group["dates"] == "October 2020 - Present"
    assert "progression" not in group  # hide_progression policy
    names = [e["name"] for e in group["engagements"]]
    assert names[0] == "Cars Platform" and set(names) <= {"Cars Platform", "Farm Network", "Factory API"}
    cars = group["engagements"][0]
    assert cars["dates"] == "October 2020 - October 2024"
    assert cars["bullets"][0].startswith("Owned and maintained 5 Spring Boot")
    assert group["bullets"] == [b for e in group["engagements"] for b in e["bullets"]]
    assert "role" not in cars  # the backend blueprint does not show project roles
    startup = payload["experience"][1]
    assert startup["company_url"] == "https://startup.example" and startup["title"] == "CTO (alongside full-time role)"
    assert startup["bullets"] == ["Technical owner of the platform, writing most commits."]


def test_build_keeps_keywords_focused_and_shows_project_roles_for_architects(full_profile):
    job = {"id": "a1", "title": "Software Architect", "company": "Target",
           "description": "Architect NestJS and PostgreSQL platforms on AWS.", "tech_required": "nestjs, postgresql, aws"}
    chosen = blueprints.choose(full_profile, job)
    selection = evidence.select(full_profile, job, chosen)
    payload = blueprints.build(full_profile, job, chosen, selection)
    assert payload["headline"] == "Software Architect | Cloud & Backend"
    group = payload["experience"][0]
    farm = next(e for e in group["engagements"] if e["name"] == "Farm Network")
    assert farm["role"] == "Software Architect"
    assert farm["tech"] and all(term in ("NestJS", "PostgreSQL", "AWS") for term in farm["tech"].split(" · "))
    assert payload["certifications"] == ["Scrum Certificate", "AWS Certificate"]
    assert payload.get("volunteering", []) == []  # no match term in this posting
    assert payload["layout"]["volunteering_section_title"] == "Teaching & Training"


def test_build_requires_languages(full_profile):
    full_profile["additional"] = {}
    job = java_job()
    chosen = blueprints.choose(full_profile, job)
    with pytest.raises(blueprints.BlueprintError, match="languages"):
        blueprints.build(full_profile, job, chosen, evidence.select(full_profile, job, chosen))


def test_reviewed_sidebar_shows_teaching_as_title_and_date_only_without_duplicates(full_profile):
    full_profile["volunteering"] = [
        {"id": "v-ensit", "title": "University Instructor", "organization": "University", "dates": "2022 - Present",
         "description": "Microservices and Spring", "confirmation": "candidate-confirmed", "confidentiality": "public",
         "match_terms": ["spring"]},
        {"id": "v-ensit", "title": "Spring Instructor", "organization": "University", "dates": "2022 - Present",
         "description": "", "confirmation": "candidate-confirmed", "confidentiality": "public", "match_terms": ["spring"]},
        {"id": "v-ai", "title": "AI Trainer", "organization": "", "dates": "10/2026", "description": "Four sessions",
         "confirmation": "candidate-confirmed", "confidentiality": "public", "match_terms": ["java"]},
    ]
    job = java_job()
    chosen = blueprints.choose(full_profile, job)
    payload = blueprints.build(full_profile, job, chosen, evidence.select(full_profile, job, chosen))
    assert [(v["title"], v["description"]) for v in payload["volunteering"]] == [("Spring Instructor", ""), ("AI Trainer", "")]
    full_profile["resume_quality"]["reviewed_sidebar"] = False
    payload = blueprints.build(full_profile, job, chosen, evidence.select(full_profile, job, chosen))
    assert [v["description"] for v in payload["volunteering"]] == ["Microservices and Spring", "", "Four sessions"]


def test_reviewed_sidebar_lists_only_the_reviewed_degree_wording(full_profile):
    full_profile["education"] = [
        {"degree": "Engineering Diploma", "school": "Example University of Technology", "dates": "09/2014 - 07/2019"},
        {"degree": "University Instructor, Spring", "school": "University", "dates": "2022 - Present"},
        {"degree": "Two-day course", "school": "Employer"},
        {"degree": "Engineering Diploma", "school": "Example University of Technology", "dates": "09/2014 - 07/2019",
         "display_school": "Example University", "display_degree": "Diploma"},
    ]
    job = java_job()
    chosen = blueprints.choose(full_profile, job)
    payload = blueprints.build(full_profile, job, chosen, evidence.select(full_profile, job, chosen))
    assert payload["education"] == [full_profile["education"][3]]  # teaching lives in Teaching & Training
    full_profile["resume_quality"]["reviewed_sidebar"] = False
    payload = blueprints.build(full_profile, job, chosen, evidence.select(full_profile, job, chosen))
    assert [e["degree"] for e in payload["education"]] == [e["degree"] for e in full_profile["education"]]
