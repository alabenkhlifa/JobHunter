import copy
import json
from datetime import datetime, timezone

import pytest

import jobhunter_interest_flow as flow
import resume_refiner as refiner
from resume_refiner import (
    ProfileValidationError,
    atomic_update_profile,
    project_public_resume,
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
