"""Structured evidence: posting needs, the evidence catalogue, scoring and deterministic selection."""

import copy

import pytest

import resume_evidence as evidence


def item(id, text, project, kind, stack=(), domains=(), experience="exp-group", **extra):
    return {"id": id, "experience_id": experience, "public_text": text, "confirmation": "candidate-confirmed",
            "confidentiality": "public", "visibility": ["resume"],
            "facets": {"project": project, "kind": kind, "stack": list(stack), "domains": list(domains)}, **extra}


@pytest.fixture
def profile():
    return {
        "name": "Candidate", "location": "Home, Country", "additional": {"languages": "English (C1)"},
        "experience": [
            {"id": "exp-group", "title": "Lead Engineer", "company": "Group Co", "dates": "October 2020 - Present",
             "bullets": []},
            {"id": "exp-cto", "title": "CTO (alongside full-time role)", "company": "Startup",
             "dates": "October 2025 - Present", "bullets": ["Technical owner of the platform, writing most commits."]},
            {"id": "exp-old", "title": "Software Engineer", "company": "Old Co", "dates": "August 2019 - September 2020",
             "bullets": ["Migrated a Java EE monolith to Spring Boot microservices with RabbitMQ."]},
        ],
        "employment_groups": [{
            "company": "Group Co", "title": "Lead Engineer", "dates": "October 2020 - Present", "confirmation": "candidate-reviewed",
            "engagements": [
                {"name": "Cars Platform", "dates": "October 2020 - October 2024", "role": "Senior Engineer",
                 "tech": "Spring Boot, Kotlin, AWS", "bullets": []},
                {"name": "Farm Network", "dates": "March 2026 - June 2026", "role": "Software Architect",
                 "tech": "NestJS, PostgreSQL, AWS", "bullets": []},
                {"name": "Factory API", "dates": "March 2025 - November 2025", "tech": "Azure", "bullets": []},
            ],
        }],
        "evidence_bank": [
            item("cars-own", "Owned and maintained 5 Spring Boot microservices in Kotlin on AWS.", "Cars Platform",
                 "ownership", ["Spring Boot", "Kotlin", "AWS", "Kubernetes"], achievement_id="cars:own", wording="full"),
            item("cars-own-short", "Owned 5 Spring Boot microservices on AWS.", "Cars Platform",
                 "ownership", ["Spring Boot", "AWS"], achievement_id="cars:own", wording="short"),
            item("cars-video", "Delivered video content end to end with a v3 bookmarks API.", "Cars Platform",
                 "delivery", ["Kotlin"]),
            item("cars-migration", "Migrated 8 production services to Spring Boot 3 with zero downtime.", "Cars Platform",
                 "migration", ["Spring Boot", "Java"]),
            item("cars-mentoring", "Onboarded and mentored 7 colleagues.", "Cars Platform", "leadership"),
            item("cars-tdd", "Developed services using TDD with JUnit and Mockito.", "Cars Platform", "quality", ["JUnit"]),
            item("farm-arch", "Architected the complete NestJS and PostgreSQL backend for 4,000 farmers.", "Farm Network",
                 "architecture", ["NestJS", "PostgreSQL"], ["agriculture", "payments"]),
            item("farm-infra", "Designed and built the AWS infrastructure with Terraform.", "Farm Network",
                 "infrastructure", ["AWS", "Terraform"]),
            item("farm-payouts", "Built mobile-money payouts that cannot pay twice, backed by 1,300 tests.", "Farm Network",
                 "reliability", ["PostgreSQL"], ["payments"]),
            item("farm-training", "Trained the team in Claude Code.", "Farm Network", "ai_tooling", ["Claude Code"]),
            item("factory-api", "Designed and built 5 MES integration endpoints in Azure API Management.", "Factory API",
                 "integration", ["Azure API Management", "Azure"], ["industrial"]),
            item("factory-api-alt", "Designed the MES integration API in Azure API Management with 5 endpoints.", "Factory API",
                 "integration", ["Azure API Management"], ["industrial"], source_evidence_ids=["factory-api"]),
            item("cto-own", "Technical owner of the platform, writing most commits.", "Startup", "ownership",
                 ["NestJS", "React"], ["advertising"], experience="exp-cto"),
            item("old-migration", "Migrated a Java EE monolith to Spring Boot microservices with RabbitMQ.", "Old Co",
                 "migration", ["Spring Boot", "Java", "RabbitMQ"], experience="exp-old"),
            {"id": "untagged", "experience_id": "exp-group", "public_text": "Did some other confirmed work.",
             "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"]},
        ],
    }


@pytest.fixture
def blueprint():
    return {"id": "backend", "confirmation": "candidate-confirmed", "families": ["backend", "platform"],
            "headline": {"default": "Lead Engineer | Java & Kotlin"}, "summary": "Fallback summary.",
            "summary_focus": ["ownership", "delivery"], "pages": 1,
            "lead_kinds": ["ownership", "delivery", "reliability"],
            "projects": {"max": 3, "bullets": {"lead": 4, "other": 2}, "pinned": []},
            "optional_companies": {"Startup": "if_leadership", "Old Co": "if_relevant"},
            "skills": {"Backend": ["Java", "Kotlin"]}, "certifications": []}


def java_job(**extra):
    return {"id": "j1", "title": "Senior Backend Engineer (Java)", "company": "Target",
            "description": "Own Java Spring Boot microservices on AWS with Kubernetes. Mentor engineers. "
                           "Ensure reliability and performance of production systems. Kafka experience welcome.",
            "tech_required": "java, spring boot, aws, kubernetes, kafka", **extra}


# ── posting needs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("title", "family", "level"), [
    ("Java Microservices Architect", "architect", "architect"),
    ("Product Architect", "architect", "architect"),
    ("Lead Software Engineer", "lead", "lead"),
    ("Senior Backend Engineer", "backend", "senior"),
    ("Senior Software Engineer - Platform Services", "platform", "senior"),
    ("Data Scientist", "other", "unspecified"),
])
def test_posting_family_and_seniority(title, family, level):
    needs = evidence.posting_needs({"title": title, "description": ""})
    assert (needs.family, needs.seniority) == (family, level)


def test_posting_needs_extract_stack_kinds_and_domains(profile):
    catalogue = evidence.catalogue(profile)
    needs = evidence.posting_needs(java_job(description=java_job()["description"] + " Smart factory and MES systems."),
                                   catalogue)
    assert {"java", "spring boot", "aws", "kubernetes"} <= needs.stack
    assert "kafka" not in needs.stack  # not a confirmed stack term, so it cannot be matched
    assert needs.kinds["leadership"] > 0 and needs.kinds["reliability"] > 0 and needs.kinds["ownership"] > 0
    assert needs.kinds.get("security", 0) == 0
    assert "industrial" in needs.domains


# ── catalogue ────────────────────────────────────────────────────────────────

def test_catalogue_reads_facets_groups_alternatives_and_tolerates_untagged(profile):
    items = {i.id: i for i in evidence.catalogue(profile)}
    assert items["farm-arch"].kind == "architecture" and items["farm-arch"].project == "Farm Network"
    assert items["farm-arch"].company == "Group Co" and items["farm-arch"].stack == ("nestjs", "postgresql")
    assert items["farm-arch"].figures == ("4,000",)
    assert items["cars-own"].group == items["cars-own-short"].group == "cars:own"
    assert items["factory-api"].group == items["factory-api-alt"].group
    assert items["untagged"].kind == "other" and items["untagged"].project == "Group Co"
    assert items["cars-own"].recency > items["old-migration"].recency


def test_catalogue_excludes_unconfirmed_private_and_non_resume_evidence(profile):
    profile["evidence_bank"][0]["confirmation"] = "draft"
    profile["evidence_bank"][2]["confidentiality"] = "private"
    profile["evidence_bank"][3]["visibility"] = ["cover-letter"]
    ids = {i.id for i in evidence.catalogue(profile)}
    assert not {"cars-own", "cars-video", "cars-migration"} & ids


# ── scoring ──────────────────────────────────────────────────────────────────

def test_scores_reward_matching_kind_stack_domain_and_lead_kinds(profile, blueprint):
    items = {i.id: i for i in evidence.catalogue(profile)}
    needs = evidence.posting_needs(java_job(), list(items.values()))
    score = {k: evidence.score(v, needs, blueprint) for k, v in items.items()}
    assert score["cars-own"] > score["cars-video"] > score["untagged"]
    assert score["cars-own"] > score["farm-infra"]           # stack overlap with the posting
    assert score["cars-mentoring"] > score["farm-training"]  # the posting asks for mentoring, not AI tooling
    architect = dict(blueprint, lead_kinds=["architecture", "infrastructure"])
    assert evidence.score(items["farm-arch"], needs, architect) > evidence.score(items["farm-arch"], needs, blueprint)


# ── selection ────────────────────────────────────────────────────────────────

def test_selection_orders_projects_by_fit_and_opens_each_with_scope(profile, blueprint):
    selection = evidence.select(profile, java_job(), blueprint)
    assert [p.name for p in selection.projects][:2] == ["Cars Platform", "Farm Network"]
    cars = selection.projects[0]
    assert cars.bullets[0].id == "cars-own"                      # ownership opens the project
    assert "cars-own-short" not in [b.id for b in cars.bullets]  # one wording per achievement
    assert len(cars.bullets) <= 4
    kinds = [b.kind for b in cars.bullets]
    assert len(set(kinds[:3])) == 3                              # the opening three cover distinct kinds
    farm = selection.projects[1]
    assert farm.bullets[0].id == "farm-arch" and len(farm.bullets) <= 2


def test_selection_picks_one_wording_per_achievement_group_and_dedupes_figures(profile, blueprint):
    profile["evidence_bank"].append(item("farm-arch-2", "Led the 4,000-farmer backend design.", "Farm Network",
                                         "architecture", ["NestJS"]))
    selection = evidence.select(profile, java_job(), dict(blueprint, projects={"max": 3, "bullets": {"lead": 6, "other": 6}}))
    ids = [b.id for p in selection.projects for b in p.bullets]
    assert "factory-api-alt" not in ids or "factory-api" not in ids
    assert not ("farm-arch" in ids and "farm-arch-2" in ids)    # the 4,000 figure appears once


def test_optional_companies_follow_the_blueprint_rules(profile, blueprint):
    backend = evidence.select(profile, java_job(), blueprint)
    assert "Startup" not in [p.company for p in backend.projects]      # if_leadership: posting is not a lead role
    assert "Old Co" in [p.company for p in backend.projects]           # if_relevant: Spring Boot overlaps
    lead_job = java_job(title="Lead Software Engineer", description="Lead a team of engineers on Java services.")
    lead = evidence.select(profile, lead_job, blueprint)
    assert "Startup" in [p.company for p in lead.projects]
    node_job = java_job(title="Senior Backend Engineer", description="NestJS and PostgreSQL only.", tech_required="nestjs")
    assert "Old Co" not in [p.company for p in evidence.select(profile, node_job, blueprint).projects]
    never = dict(blueprint, optional_companies={"Startup": "never", "Old Co": "never"})
    assert {p.company for p in evidence.select(profile, lead_job, never).projects} == {"Group Co"}


def test_pinned_projects_lead_and_weak_projects_are_left_out(profile, blueprint):
    pinned = dict(blueprint, projects={"max": 3, "bullets": {"lead": 3, "other": 2}, "pinned": ["Farm Network"]})
    selection = evidence.select(profile, java_job(), pinned)
    assert selection.projects[0].name == "Farm Network"
    azure_job = java_job(title="Senior Backend Engineer", description="Azure API Management integration work.",
                         tech_required="azure, azure api management")
    names = [p.name for p in evidence.select(profile, azure_job, blueprint).projects]
    assert names[0] == "Factory API"
    assert "Cars Platform" in names                                # still relevant work, not dropped
    assert all(p.bullets for p in evidence.select(profile, azure_job, blueprint).projects)


def test_selection_never_invents_or_rewrites_wording(profile, blueprint):
    selection = evidence.select(profile, java_job(), blueprint)
    confirmed = {e["public_text"] for e in profile["evidence_bank"]}
    assert all(b.text in confirmed for p in selection.projects for b in p.bullets)
    assert selection.needs.family == "backend"
    assert copy.deepcopy(profile) == profile
