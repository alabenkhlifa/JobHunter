import copy

import pytest

import resume_blocks


@pytest.fixture
def profile():
    return {
        "name": "Candidate",
        "email": "candidate@example.test",
        "location": "Madrid, Spain",
        "additional": {"languages": "English (C1)"},
        "resume_blocks": {
            "version": 1,
            "enabled": True,
            "base": {"name": "Candidate", "email": "candidate@example.test", "location": "Madrid, Spain",
                     "additional": {"languages": "English (C1)"}, "skills": {"Backend": ["Java"]}},
            "flavors": [{"id": "software-engineer", "label": "Software Engineer",
                         "confirmation": "candidate-confirmed", "headline": "Software Engineer | Java",
                         "summary": "Approved software engineering summary."},
                        {"id": "architect", "label": "Architect", "confirmation": "draft",
                         "headline": "Architect", "summary": "Draft."}],
            "projects": [
                {"id": "cars", "label": "Cars Platform", "confirmation": "candidate-confirmed", "company": "Group Co",
                 "role": {"title": "Lead Engineer", "dates": "2020 - Present", "location": "Madrid"},
                 "entry": {"name": "Cars Platform", "dates": "2021 - 2024", "role": "Senior Engineer",
                           "tech": "Java, Spring", "bullets": ["Owned five Java services.", "Migrated eight services."]}},
                {"id": "farm", "label": "Farm Network", "confirmation": "candidate-confirmed", "company": "Group Co",
                 "role": {"title": "Lead Engineer", "dates": "2020 - Present", "location": "Madrid"},
                 "entry": {"name": "Farm Network", "dates": "2025 - 2026", "role": "Software Architect",
                           "tech": "NestJS", "bullets": ["Designed the confirmed platform."]}},
                {"id": "startup", "label": "Startup", "confirmation": "draft", "company": "Startup",
                 "role": {"title": "CTO", "dates": "2025 - Present", "bullets": ["Draft bullet."]}, "entry": None},
            ],
            "variants": [{"id": "software-engineer-core", "label": "Core projects", "flavor_id": "software-engineer",
                          "confirmation": "candidate-confirmed", "max_pages": 2,
                          "project_ids": ["cars", "farm"], "default_project_ids": ["cars"]}],
        },
    }


def test_build_copies_exact_approved_blocks_and_keeps_selected_order(profile):
    result = resume_blocks.build(profile, {"flavor_id": "software-engineer", "variant_id": "software-engineer-core",
                                           "project_ids": ["farm", "cars"]})
    assert result["headline"] == "Software Engineer | Java"
    assert result["summary"] == "Approved software engineering summary."
    assert result["experience"][0]["company"] == "Group Co"
    assert [entry["name"] for entry in result["experience"][0]["engagements"]] == ["Farm Network", "Cars Platform"]
    assert result["experience"][0]["bullets"] == [
        "Designed the confirmed platform.", "Owned five Java services.", "Migrated eight services."]
    assert "resume_blocks" not in result


@pytest.mark.parametrize("selection", [
    None,
    {"flavor_id": "architect", "variant_id": "software-engineer-core", "project_ids": ["cars"]},
    {"flavor_id": "software-engineer", "variant_id": "software-engineer-core", "project_ids": []},
    {"flavor_id": "software-engineer", "variant_id": "software-engineer-core", "project_ids": ["startup"]},
    {"flavor_id": "software-engineer", "variant_id": "software-engineer-core", "project_ids": ["cars", "cars"]},
])
def test_build_rejects_missing_unconfirmed_duplicate_and_disallowed_choices(profile, selection):
    with pytest.raises(resume_blocks.ResumeBlocksError):
        resume_blocks.build(profile, selection)


def test_catalog_rejects_unapproved_bullet_list_and_private_renderer_field(profile):
    changed = copy.deepcopy(profile)
    changed["resume_blocks"]["projects"][0]["entry"]["bullets"] = []
    with pytest.raises(resume_blocks.ResumeBlocksError, match="bullets"):
        resume_blocks.catalog(changed)
    changed = copy.deepcopy(profile)
    changed["resume_blocks"]["base"]["candidate_notes"] = "private"
    with pytest.raises(resume_blocks.ResumeBlocksError, match="renderer fields"):
        resume_blocks.catalog(changed)


def test_draft_flavor_can_wait_for_approval_without_public_wording(profile):
    changed = copy.deepcopy(profile)
    changed["resume_blocks"]["flavors"][1] = {"id": "architect", "label": "Architect",
                                               "confirmation": "unconfirmed", "headline": "", "summary": ""}
    assert resume_blocks.catalog(changed)["flavors"][1]["headline"] == ""
    with pytest.raises(resume_blocks.ResumeBlocksError, match="candidate-confirmed resume flavor"):
        resume_blocks.build(changed, {"flavor_id": "architect", "variant_id": "software-engineer-core",
                                      "project_ids": ["cars"]})


def test_catalog_rejects_unapproved_nested_renderer_data(profile):
    changed = copy.deepcopy(profile)
    changed["resume_blocks"]["base"]["additional"]["private_notes"] = "private"
    with pytest.raises(resume_blocks.ResumeBlocksError, match="additional"):
        resume_blocks.catalog(changed)
    changed = copy.deepcopy(profile)
    changed["resume_blocks"]["projects"][0]["role"]["internal_note"] = "private"
    with pytest.raises(resume_blocks.ResumeBlocksError, match="role has invalid employer fields"):
        resume_blocks.catalog(changed)


def test_enabled_catalog_with_invalid_contents_fails_instead_of_falling_back(profile):
    changed = copy.deepcopy(profile)
    changed["resume_blocks"]["projects"][0]["entry"]["bullets"] = []
    with pytest.raises(resume_blocks.ResumeBlocksError, match="bullets"):
        resume_blocks.enabled(changed)


def test_standalone_role_preserves_renderer_fields_and_bullets(profile):
    changed = copy.deepcopy(profile)
    standalone = {"id": "solo", "label": "Startup", "confirmation": "candidate-confirmed", "company": "Startup",
                  "role": {"title": "CTO", "dates": "2025 - Present", "subtitle": "Alongside full-time role",
                           "tech": "NestJS", "company_url": "https://startup.example",
                           "bullets": ["Technical owner of the platform."]}, "entry": None}
    changed["resume_blocks"]["projects"].append(standalone)
    changed["resume_blocks"]["variants"][0]["project_ids"].append("solo")
    result = resume_blocks.build(changed, {"flavor_id": "software-engineer", "variant_id": "software-engineer-core",
                                           "project_ids": ["solo"]})
    role = result["experience"][0]
    assert role["subtitle"] == "Alongside full-time role" and role["tech"] == "NestJS"
    assert role["company_url"] == "https://startup.example"
    assert role["bullets"] == ["Technical owner of the platform."]


def test_fit_compacts_without_dropping_content_and_reports_sparse_second_page():
    payload = {"experience": [{"bullets": ["Fixed bullet"]}]}
    calls = []

    def render(value):
        calls.append(copy.deepcopy(value))
        return {"pages": 2, "main_fills": [0.8, 0.3]} if len(calls) == 1 else {"pages": 2, "main_fills": [0.8, 0.4]}

    fitted, report = resume_blocks.fit(payload, render, 2)
    assert fitted["experience"] == payload["experience"]
    assert fitted["layout"]["compact_spacing"] is True
    assert report["page_two_sparse"] is True
    assert "under half full" in report["action_warning"]


def test_fit_rejects_overflow_after_spacing_compaction():
    calls = []

    def render(_value):
        calls.append(1)
        return {"pages": 3, "main_fills": [0.8, 0.8, 0.3]}

    with pytest.raises(resume_blocks.ResumeBlocksError, match="above the selected 2-page limit"):
        resume_blocks.fit({"experience": [{"bullets": ["Fixed"]}]}, render, 2)
    assert len(calls) == 2


def test_variant_overrides_preserve_other_variants_and_source_blocks(profile):
    selection = {"flavor_id": "software-engineer", "variant_id": "software-engineer-core", "project_ids": ["cars"]}
    original = copy.deepcopy(profile)
    before = resume_blocks.build(profile, selection)
    scoped = copy.deepcopy(profile["resume_blocks"]["variants"][0])
    scoped.update(id="detailed", base_overrides={"skills": {"Leadership": ["Mentoring"]},
                                               "layout": {"personal_details_page_two": True}},
                  project_overrides={"cars": copy.deepcopy(profile["resume_blocks"]["projects"][0])})
    scoped["project_overrides"]["cars"]["entry"]["bullets"] = ["Designed the scoped service architecture."]
    profile["resume_blocks"]["variants"].append(scoped)
    result = resume_blocks.build(profile, {**selection, "variant_id": "detailed"})
    assert result["skills"] == {"Leadership": ["Mentoring"]}
    assert result["layout"]["personal_details_page_two"] is True
    assert result["experience"][0]["bullets"] == ["Designed the scoped service architecture."]
    assert resume_blocks.build(profile, selection) == before
    assert profile["resume_blocks"]["base"] == original["resume_blocks"]["base"]
    assert profile["resume_blocks"]["projects"] == original["resume_blocks"]["projects"]
    result["experience"][0]["engagements"][0]["bullets"].clear()
    assert scoped["project_overrides"]["cars"]["entry"]["bullets"]


@pytest.mark.parametrize("overrides", [
    {"base_overrides": {"private_notes": "Private"}},
    {"base_overrides": {"layout": {"personal_details_page_two": "yes"}}},
    {"base_overrides": {"layout": {"page_break_before_engagement": 2}}},
    {"project_overrides": {"unknown": {"id": "unknown"}}},
    {"project_overrides": {"cars": {"id": "other"}}},
])
def test_variant_overrides_reject_private_invalid_and_disallowed_data(profile, overrides):
    profile["resume_blocks"]["variants"][0].update(overrides)
    with pytest.raises(resume_blocks.ResumeBlocksError):
        resume_blocks.catalog(profile)


def test_production_rejects_unconfirmed_project_override(profile):
    override = copy.deepcopy(profile["resume_blocks"]["projects"][0])
    override["confirmation"] = "draft"
    profile["resume_blocks"]["variants"][0]["project_overrides"] = {"cars": override}
    with pytest.raises(resume_blocks.ResumeBlocksError, match="override is not candidate-confirmed"):
        resume_blocks.build(profile, {"flavor_id": "software-engineer", "variant_id": "software-engineer-core",
                                      "project_ids": ["cars"]})


def test_confirmed_variant_project_does_not_require_confirming_its_shared_original(profile):
    shared = profile['resume_blocks']['projects'][0]
    scoped = copy.deepcopy(shared)
    scoped['entry']['bullets'] = ['Approved variant-specific wording.']
    shared['confirmation'] = 'draft'
    variant = profile['resume_blocks']['variants'][0]
    variant['project_overrides'] = {'cars': scoped}
    result = resume_blocks.build(profile, {'flavor_id': variant['flavor_id'], 'variant_id': variant['id'], 'project_ids': ['cars']})
    assert result['experience'][0]['bullets'] == scoped['entry']['bullets']
    assert shared['confirmation'] == 'draft' and shared['entry']['bullets'] != scoped['entry']['bullets']
