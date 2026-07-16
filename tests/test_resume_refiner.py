import copy
import json
from datetime import datetime, timezone

import pytest

import jobhunter_interest_flow as flow
import resume_refiner as refiner
from resume_refiner import (
    ProfileValidationError,
    apply_resume_variant,
    atomic_update_profile,
    project_public_resume,
    select_resume_variant,
    usable_evidence,
    validate_profile,
)


def _experience(**overrides):
    experience = {
        "id": "exp-example",
        "title": "Senior Software Engineer",
        "company": "Example",
        "dates": "01/2021 - Present",
        "bullets": ["Maintained Kotlin and Java backend services."],
        "tech": "Kotlin - Java - Spring Boot",
    }
    experience.update(overrides)
    return experience


def _evidence(evidence_id="ev-public", **overrides):
    item = {
        "id": evidence_id,
        "experience_id": "exp-example",
        "public_text": "Implemented asynchronous task processing in a sample production service.",
        "confirmation": "candidate-confirmed",
        "confidentiality": "public",
        "visibility": ["resume", "cover-letter"],
    }
    item.update(overrides)
    return item


def _v2_profile(**overrides):
    profile = {
        "name": "Candidate",
        "headline": "Senior Backend Engineer",
        "summary": "Backend engineer with 8+ years of experience.",
        "skills": {"Backend": ["Kotlin", "Java", "Spring Boot"]},
        "experience": [_experience()],
        "education": [],
        "evidence_bank": [_evidence()],
    }
    profile.update(overrides)
    return profile


def _backend_job():
    return {
        "title": "Backend Software Engineer",
        "company": "TargetCo",
        "description": "Build asynchronous Java task-processing services for a sample product.",
    }


def _variant(variant_id="variant-java", **overrides):
    variant = {
        "id": variant_id,
        "confirmation": "candidate-confirmed",
        "match_terms": ["java"],
        "resume": {"headline": "Java Backend Engineer"},
    }
    variant.update(overrides)
    return variant


def test_legacy_profile_keeps_existing_tailoring_behavior():
    profile = {
        "name": "Candidate",
        "headline": "Backend Engineer",
        "summary": "Backend engineer. Built mobile apps. Owned Java services. Mentored engineers.",
        "skills": {"Backend": ["Kotlin", "Java", "Spring Boot"]},
        "experience": [
            {
                "title": "Senior Engineer",
                "company": "Example",
                "dates": "2020 - Present",
                "bullets": ["Built mobile screens.", "Owned Java backend services.", "Mentored engineers."],
                "tech": "Java - Spring Boot",
            }
        ],
        "education": [],
    }

    validate_profile(profile)
    resume = flow._tailor_resume(profile, _backend_job())

    assert select_resume_variant(profile, "Java backend") is None
    assert project_public_resume(profile) == profile
    assert resume["headline"] == profile["headline"]
    assert resume["experience"][0]["tech"] == profile["experience"][0]["tech"]
    assert set(resume["experience"][0]["bullets"]) == set(profile["experience"][0]["bullets"])


def test_only_confirmed_public_matching_evidence_is_used_verbatim():
    exact_text = "Implemented asynchronous task processing in a sample production service."
    profile = _v2_profile(
        evidence_bank=[
            _evidence(public_text=exact_text),
            _evidence(
                "ev-draft",
                public_text="DRAFT EVIDENCE MUST NOT LEAK.",
                confirmation="draft",
            ),
            _evidence(
                "ev-private",
                public_text="PRIVATE EVIDENCE MUST NOT LEAK.",
                confidentiality="private",
            ),
            _evidence(
                "ev-interview",
                public_text="INTERVIEW EVIDENCE MUST NOT LEAK.",
                visibility="interview-only",
            ),
            _evidence(
                "ev-cover",
                public_text="Upgraded a sample Java service incrementally.",
                visibility="cover-letter",
            ),
        ]
    )

    resume = flow._tailor_resume(profile, _backend_job())
    letter = flow._cover_letter(profile, _backend_job())
    resume_text = json.dumps(resume, ensure_ascii=False)
    letter_text = json.dumps(letter, ensure_ascii=False)

    assert exact_text in resume["experience"][0]["bullets"]
    assert any(highlight["text"] == exact_text for highlight in letter["highlights"])
    assert exact_text in resume_text and exact_text in letter_text
    assert "Upgraded a sample Java service incrementally." not in resume_text
    for forbidden in ("DRAFT EVIDENCE", "PRIVATE EVIDENCE", "INTERVIEW EVIDENCE"):
        assert forbidden not in resume_text
        assert forbidden not in letter_text


def test_exact_duplicate_evidence_is_emitted_once():
    text = "Implemented asynchronous task processing in a sample production service."
    profile = _v2_profile(
        experience=[_experience(bullets=[text])],
        evidence_bank=[_evidence(public_text=text)],
    )

    resume = flow._tailor_resume(profile, _backend_job())
    letter = flow._cover_letter(profile, _backend_job())

    assert resume["experience"][0]["bullets"].count(text) == 1
    assert [item["text"] for item in letter["highlights"]].count(text) == 1


def test_public_projection_excludes_refiner_defaults_and_unknown_private_metadata():
    profile = _v2_profile()
    profile.update(
        {
            "application_defaults": {"requires_visa_sponsorship": "private"},
            "refiner_session": {"transcript": "private"},
            "private_notes": "private",
        }
    )
    profile["experience"][0]["manager_name"] = "private"
    profile["education"] = [
        {
            "degree": "Degree",
            "school": "School",
            "dates": "2010 - 2014",
            "private_verification": "private",
        }
    ]

    projected = project_public_resume(profile)
    resume = flow._tailor_resume(profile, _backend_job())

    for payload in (projected, resume):
        serialized = json.dumps(payload)
        assert "evidence_bank" not in payload
        assert "application_defaults" not in payload
        assert "refiner_session" not in payload
        assert "private_notes" not in payload
        assert "manager_name" not in serialized
        assert "private_verification" not in serialized
    assert resume["experience"][0]["tech"] == "Kotlin - Java - Spring Boot"


def test_resume_variant_selection_is_confirmed_whole_term_and_deterministic():
    variants = [
        _variant("variant-two-low", match_terms=["java", "spring boot"], priority=1),
        _variant("variant-two-high-first", match_terms=["java", "spring boot"], priority=5),
        _variant("variant-two-high-second", match_terms=["java", "spring boot"], priority=5),
        _variant("variant-one", match_terms=["java"], priority=100),
        _variant(
            "variant-draft",
            confirmation="draft",
            match_terms=["java", "spring boot", "apis"],
            priority=1000,
        ),
        _variant(
            "variant-unconfirmed",
            confirmation="unconfirmed",
            match_terms=["java", "spring boot", "apis"],
            priority=1000,
        ),
    ]
    profile = _v2_profile(resume_variants=variants)

    selected = select_resume_variant(
        profile,
        "A JavaScript runtime plus Java APIs built with SPRING-BOOT.",
    )

    assert selected["id"] == "variant-two-high-first"
    selected["resume"]["headline"] = "Changed copy"
    assert profile["resume_variants"][1]["resume"]["headline"] == "Java Backend Engineer"

    java_only = _v2_profile(resume_variants=[_variant(match_terms=["java"])])
    assert select_resume_variant(java_only, "JavaScript and TypeScript") is None


def test_draft_and_unconfirmed_resume_variants_are_never_selected_or_applied():
    profile = _v2_profile(
        resume_variants=[
            _variant("variant-draft", confirmation="draft"),
            _variant("variant-unconfirmed", confirmation="unconfirmed"),
        ]
    )

    assert select_resume_variant(profile, "Java backend") is None
    with pytest.raises(ProfileValidationError, match="candidate-confirmed"):
        apply_resume_variant(profile, profile["resume_variants"][0])


def test_nonmatching_confirmed_variant_keeps_legacy_tailoring_fallback():
    profile = _v2_profile(
        resume_variants=[_variant(match_terms=["java"])],
    )
    job = {
        "title": "Cloud Engineer",
        "company": "TargetCo",
        "description": "Build Azure and Terraform infrastructure.",
    }

    resume, variant = flow._resume_for_job(profile, job)

    assert variant is None
    assert resume["headline"] == profile["headline"]
    assert resume["experience"][0]["company"] == profile["experience"][0]["company"]


def test_apply_resume_variant_preserves_exact_content_order_and_omits_metadata():
    exact_experience = [
        {
            "title": "Lead Engineer",
            "company": "Company B",
            "dates": "2022 - Present",
            "bullets": ["Second fact stays first.", "First fact stays second."],
            "tech": "Java - Kotlin",
        },
        {
            "title": "Engineer",
            "company": "Company A",
            "dates": "2020 - 2022",
            "bullets": ["Earlier experience remains second."],
        },
    ]
    variant = _variant(
        match_terms=["backend"],
        priority=7,
        max_pages=2,
        omit_sections=["summary", "skills", "additional"],
        resume={
            "headline": "Confirmed Backend Headline",
            "summary": "This section is intentionally omitted.",
            "certifications": ["Certification B", "Certification A"],
            "skills": {"Backend": ["Kotlin", "Java"]},
            "experience": exact_experience,
            "education": [
                {"degree": "Degree B", "school": "School B", "dates": "2014 - 2016"},
                {"degree": "Degree A", "school": "School A", "dates": "2010 - 2014"},
            ],
            "additional": {"languages": "English"},
        },
    )
    profile = _v2_profile(
        email="candidate@example.com",
        phone="+1 555 0100",
        resume_variants=[variant],
        private_notes="must not leak",
    )

    selected = select_resume_variant(profile, "Backend services")
    result = apply_resume_variant(profile, selected)

    assert "resume_variants" not in project_public_resume(profile)
    assert result["name"] == "Candidate"
    assert result["email"] == "candidate@example.com"
    assert result["phone"] == "+1 555 0100"
    assert result["headline"] == "Confirmed Backend Headline"
    assert result["certifications"] == ["Certification B", "Certification A"]
    assert result["experience"] == exact_experience
    assert [item["degree"] for item in result["education"]] == ["Degree B", "Degree A"]
    assert "summary" not in result
    assert "skills" not in result
    assert "additional" not in result
    for metadata in (
        "resume_variants",
        "id",
        "confirmation",
        "match_terms",
        "priority",
        "max_pages",
        "omit_sections",
        "private_notes",
    ):
        assert metadata not in result

    result["experience"][0]["bullets"][0] = "Changed output"
    assert variant["resume"]["experience"][0]["bullets"][0] == "Second fact stays first."


def test_resume_variant_does_not_inherit_unspecified_master_sections():
    variant = _variant(resume={"headline": "Confirmed Java Backend Engineer"})
    profile = _v2_profile(
        resume_variants=[variant],
        certifications=["Master-only certification"],
        additional={"interests": "Master-only interests"},
    )

    result = apply_resume_variant(profile, select_resume_variant(profile, "Java services"))

    assert result == {
        "name": "Candidate",
        "headline": "Confirmed Java Backend Engineer",
    }


@pytest.mark.parametrize("identity_field", ["name", "email", "phone", "linkedin", "location"])
def test_resume_variant_rejects_identity_and_contact_overrides(identity_field):
    profile = _v2_profile(
        resume_variants=[_variant(resume={identity_field: "untrusted override"})]
    )

    with pytest.raises(ProfileValidationError, match="identity"):
        validate_profile(profile)


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda p: p.update(resume_variants={}), "resume_variants must be a list"),
        (lambda p: p["resume_variants"][0].update(id="bad id"), "id is missing or malformed"),
        (
            lambda p: p["resume_variants"].append(copy.deepcopy(p["resume_variants"][0])),
            "id is duplicated",
        ),
        (lambda p: p["resume_variants"][0].update(confirmation="assumed"), "confirmation"),
        (lambda p: p["resume_variants"][0].update(match_terms=[]), "non-empty list"),
        (lambda p: p["resume_variants"][0].update(match_terms=["Java", " java "]), "unique"),
        (lambda p: p["resume_variants"][0].update(priority=True), "priority must be an integer"),
        (lambda p: p["resume_variants"][0].update(max_pages=0), "max_pages must be a positive"),
        (lambda p: p["resume_variants"][0].update(omit_sections=["contact"]), "unsupported section"),
        (
            lambda p: p["resume_variants"][0].update(omit_sections=["skills", "skills"]),
            "contains duplicates",
        ),
        (lambda p: p["resume_variants"][0].update(resume=[]), "resume must be an object"),
        (
            lambda p: p["resume_variants"][0]["resume"].update(private_notes="private"),
            "unsupported, identity, or private",
        ),
        (
            lambda p: p["resume_variants"][0]["resume"].update(
                experience=[{"title": "Engineer", "private_notes": "private"}]
            ),
            "unsupported or private",
        ),
        (
            lambda p: p["resume_variants"][0]["resume"].update(
                education=[{"degree": "Degree", "private_notes": "private"}]
            ),
            "unsupported or private",
        ),
        (
            lambda p: p["resume_variants"][0]["resume"].update(
                additional={"languages": ["English"]}
            ),
            "fields must be strings",
        ),
    ],
)
def test_invalid_resume_variants_are_rejected(mutate, error):
    profile = _v2_profile(resume_variants=[_variant()])
    mutate(profile)

    with pytest.raises(ProfileValidationError, match=error):
        validate_profile(profile)


def test_atomic_update_upgrades_legacy_profile_preserves_unknown_fields_and_backs_up(tmp_path):
    path = tmp_path / "master-profile.json"
    legacy = {
        "name": "Candidate",
        "unknown_private": {"keep": True},
        "unknown_list": ["keep"],
        "certifications": ["Existing Certification"],
        "skills": {"Backend": ["Java", "Spring Boot"]},
        "experience": [
            {
                "title": "Senior Software Engineer",
                "company": "Example",
                "dates": "01/2021 - Present",
                "bullets": ["Maintained backend services."],
                "private_record": "keep",
            }
        ],
        "education": [
            {
                "degree": "Existing Degree",
                "school": "Existing School",
                "dates": "2010 - 2014",
            }
        ],
    }
    original_text = json.dumps(legacy, indent=2)
    path.write_text(original_text, encoding="utf-8")
    updates = {
        "certifications": ["New Certification"],
        "skills": {"Backend": ["Spring Boot", "Kotlin"]},
        "experience": [
            {
                "id": "exp-example",
                "bullets": ["Maintained backend services.", "Migrated services incrementally."],
            }
        ],
        "education": [
            {
                "degree": "New Degree",
                "school": "New School",
                "dates": "2015 - 2017",
            }
        ],
        "evidence_bank": [_evidence()],
        "refiner_metadata": {"last_completed_experience_id": "exp-example"},
    }

    backup = atomic_update_profile(
        path,
        updates,
        candidate_confirmed=True,
        now=datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc),
    )
    updated = json.loads(path.read_text(encoding="utf-8"))

    assert backup.name == "master-profile.json.backup-20260716T040000Z"
    assert backup.read_text(encoding="utf-8") == original_text
    assert updated["unknown_private"] == {"keep": True}
    assert updated["unknown_list"] == ["keep"]
    assert updated["certifications"] == ["Existing Certification", "New Certification"]
    assert updated["skills"]["Backend"] == ["Java", "Spring Boot", "Kotlin"]
    assert updated["experience"][0]["private_record"] == "keep"
    assert updated["experience"][0]["id"] == "exp-example"
    assert updated["experience"][0]["bullets"] == [
        "Maintained backend services.",
        "Migrated services incrementally.",
    ]
    assert [item["degree"] for item in updated["education"]] == ["Existing Degree", "New Degree"]
    assert updated["evidence_bank"][0]["public_text"] == _evidence()["public_text"]
    assert not list(tmp_path.glob(".master-profile.json.*.new"))


def test_atomic_update_cannot_mark_evidence_confirmed_without_confirmation(tmp_path):
    path = tmp_path / "master-profile.json"
    profile = _v2_profile(evidence_bank=[])
    path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="explicit candidate confirmation"):
        atomic_update_profile(
            path,
            {"evidence_bank": [_evidence()]},
            candidate_confirmed=False,
        )

    assert json.loads(path.read_text(encoding="utf-8")) == profile
    assert not list(tmp_path.glob("*.backup-*"))


def test_atomic_update_cannot_change_public_facts_without_confirmation(tmp_path):
    path = tmp_path / "master-profile.json"
    profile = _v2_profile()
    path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="Public resume fields"):
        atomic_update_profile(
            path,
            {"headline": "Unconfirmed new headline"},
            candidate_confirmed=False,
        )
    with pytest.raises(ProfileValidationError, match="Usable public evidence"):
        atomic_update_profile(
            path,
            {"evidence_bank": [{"id": "ev-public", "public_text": "Unconfirmed replacement."}]},
            candidate_confirmed=False,
        )
    with pytest.raises(ProfileValidationError, match="Usable public evidence"):
        atomic_update_profile(
            path,
            {"evidence_bank": [{"id": "ev-public", "confirmation": "draft"}]},
            candidate_confirmed=False,
        )

    assert json.loads(path.read_text(encoding="utf-8")) == profile
    assert not list(tmp_path.glob("*.backup-*"))


def test_draft_update_is_stored_but_not_usable_without_confirmation(tmp_path):
    path = tmp_path / "master-profile.json"
    profile = _v2_profile(evidence_bank=[])
    path.write_text(json.dumps(profile), encoding="utf-8")
    draft = _evidence(confirmation="unconfirmed")

    atomic_update_profile(
        path,
        {"evidence_bank": [draft]},
        candidate_confirmed=False,
    )
    updated = json.loads(path.read_text(encoding="utf-8"))

    assert updated["evidence_bank"] == [draft]
    assert usable_evidence(updated, "resume") == []
    assert usable_evidence(updated, "cover-letter") == []


def test_atomic_update_preserves_evidence_list_metadata_while_replacing_confirmed_text(tmp_path):
    path = tmp_path / "master-profile.json"
    original_evidence = _evidence(
        role_tags=["backend"],
        visibility="resume",
    )
    profile = _v2_profile(evidence_bank=[original_evidence])
    path.write_text(json.dumps(profile), encoding="utf-8")

    atomic_update_profile(
        path,
        {
            "evidence_bank": [
                {
                    "id": "ev-public",
                    "public_text": "Candidate-confirmed replacement text.",
                    "role_tags": ["backend", "reliability"],
                    "visibility": ["cover-letter"],
                }
            ]
        },
        candidate_confirmed=True,
    )
    updated = json.loads(path.read_text(encoding="utf-8"))
    evidence = updated["evidence_bank"][0]

    assert evidence["public_text"] == "Candidate-confirmed replacement text."
    assert evidence["role_tags"] == ["backend", "reliability"]
    assert evidence["visibility"] == ["resume", "cover-letter"]


def test_atomic_update_replaces_resume_variants_by_id_without_duplicates(tmp_path):
    path = tmp_path / "master-profile.json"
    existing = _variant(
        match_terms=["java"],
        resume={"headline": "Java Backend Engineer"},
    )
    profile = _v2_profile(
        resume_variants=[existing],
        unknown_private={"keep": True},
    )
    path.write_text(json.dumps(profile), encoding="utf-8")

    replacement = _variant(
        "variant-java",
        match_terms=["spring boot"],
        resume={"summary": "Confirmed Java and Spring background."},
    )
    atomic_update_profile(
        path,
        {
            "resume_variants": [
                replacement,
                _variant(
                    "variant-cloud",
                    confirmation="draft",
                    match_terms=["cloud"],
                    resume={"headline": "Cloud Engineer"},
                ),
            ]
        },
        candidate_confirmed=True,
    )
    updated = json.loads(path.read_text(encoding="utf-8"))

    assert [variant["id"] for variant in updated["resume_variants"]] == [
        "variant-java",
        "variant-cloud",
    ]
    merged = updated["resume_variants"][0]
    assert merged == replacement
    assert updated["unknown_private"] == {"keep": True}


def test_atomic_update_cannot_create_or_replace_confirmed_variant_without_confirmation(tmp_path):
    path = tmp_path / "master-profile.json"
    profile = _v2_profile(resume_variants=[_variant()])
    path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="cannot be candidate-confirmed"):
        atomic_update_profile(
            path,
            {"resume_variants": [_variant("variant-new")]},
            candidate_confirmed=False,
        )
    with pytest.raises(ProfileValidationError, match="Usable resume variants"):
        atomic_update_profile(
            path,
            {
                "resume_variants": [
                    _variant(
                        "variant-java",
                        confirmation="draft",
                    )
                ]
            },
            candidate_confirmed=False,
        )
    with pytest.raises(ProfileValidationError, match="cannot be candidate-confirmed"):
        atomic_update_profile(
            path,
            {
                "resume_variants": [
                    _variant(
                        "variant-java",
                        resume={"summary": "Unconfirmed summary."},
                    )
                ]
            },
            candidate_confirmed=False,
        )

    assert json.loads(path.read_text(encoding="utf-8")) == profile
    assert not list(tmp_path.glob("*.backup-*"))


def test_atomic_update_promotes_interview_only_evidence_without_conflicting_visibility(tmp_path):
    path = tmp_path / "master-profile.json"
    profile = _v2_profile(evidence_bank=[_evidence(visibility="interview-only")])
    path.write_text(json.dumps(profile), encoding="utf-8")

    atomic_update_profile(
        path,
        {"evidence_bank": [{"id": "ev-public", "visibility": ["resume"]}]},
        candidate_confirmed=True,
    )

    evidence = json.loads(path.read_text(encoding="utf-8"))["evidence_bank"][0]
    assert evidence["visibility"] == ["resume"]


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda p: p["experience"][0].update(id="bad id"), "id is missing or malformed"),
        (lambda p: p["experience"].append(copy.deepcopy(p["experience"][0])), "id is duplicated"),
        (lambda p: p["experience"][0].pop("title"), "title is required"),
        (lambda p: p["evidence_bank"].append(copy.deepcopy(p["evidence_bank"][0])), "id is duplicated"),
        (lambda p: p["evidence_bank"][0].update(experience_id="exp-missing"), "does not reference"),
        (lambda p: p["evidence_bank"][0].update(public_text="  "), "non-empty string"),
        (lambda p: p["evidence_bank"][0].update(confirmation="assumed"), "unsupported value"),
        (lambda p: p["evidence_bank"][0].update(confidentiality="secret"), "unsupported value"),
        (lambda p: p["evidence_bank"][0].update(visibility="linkedin"), "unsupported value"),
        (
            lambda p: p["evidence_bank"][0].update(visibility=["resume", "interview-only"]),
            "cannot combine interview-only",
        ),
    ],
)
def test_invalid_v2_profiles_are_rejected_without_echoing_evidence(mutate, error):
    profile = _v2_profile()
    mutate(profile)

    with pytest.raises(ProfileValidationError, match=error) as caught:
        validate_profile(profile)

    assert "Implemented asynchronous" not in str(caught.value)


def test_validate_cli_reports_structure_without_candidate_values(tmp_path, capsys):
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(_v2_profile()), encoding="utf-8")

    assert refiner.main(["validate", str(valid_path)]) == 0
    success = capsys.readouterr()
    assert success.out.strip() == "Profile is valid."
    assert success.err == ""

    invalid = _v2_profile()
    invalid["evidence_bank"][0]["visibility"] = "SECRET CANDIDATE VALUE"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")

    assert refiner.main(["validate", str(invalid_path)]) == 2
    failure = capsys.readouterr()
    assert "evidence_bank[0].visibility" in failure.err
    assert "SECRET CANDIDATE VALUE" not in failure.err
