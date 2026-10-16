"""The one-time profile migration that adds evidence facets, indexed wording and blueprints."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import migrate_profile_blueprints as migrate  # noqa: E402
from test_resume_evidence import profile  # noqa: F401


def updates_for(profile):
    return {
        "facets": {"untagged": {"project": "Cars Platform", "kind": "quality", "stack": ["JUnit"], "domains": []}},
        "new_evidence": [{"id": "old-indexed", "experience_id": "exp-old",
                          "public_text": "Migrated a Java EE monolith to Spring Boot microservices with RabbitMQ.",
                          "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"],
                          "source": "indexed-from-confirmed-experience-2026-10-16",
                          "facets": {"project": "Old Co", "kind": "migration", "stack": ["Spring Boot"], "domains": []}}],
        "engagement_roles": {"Group Co": {"Factory API": "Lead Engineer"}},
        "resume_blueprints": [{"id": "backend", "confirmation": "candidate-confirmed", "families": ["backend"],
                               "headline": {"default": "Lead Engineer"}, "summary": "Fallback.", "pages": 1}],
    }


def test_migration_is_additive_idempotent_and_backs_up(profile, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    updates = tmp_path / "updates.json"
    updates.write_text(json.dumps(updates_for(profile)))
    report = migrate.main(["--profile", str(path), "--updates", str(updates)])
    assert report == 0 and json.loads(path.read_text()) == profile  # dry run changes nothing
    assert migrate.main(["--profile", str(path), "--updates", str(updates), "--apply"]) == 0
    migrated = json.loads(path.read_text())
    bank = {e["id"]: e for e in migrated["evidence_bank"]}
    assert bank["untagged"]["facets"]["kind"] == "quality"
    assert bank["old-indexed"]["source"].startswith("indexed-from")
    assert migrated["employment_groups"][0]["engagements"][2]["role"] == "Lead Engineer"
    assert migrated["resume_blueprints"][0]["id"] == "backend"
    assert len(list(tmp_path.glob("profile.json.backup-*"))) == 1
    assert migrate.main(["--profile", str(path), "--updates", str(updates), "--apply"]) == 0
    again = json.loads(path.read_text())
    assert again == migrated and len(again["evidence_bank"]) == len(migrated["evidence_bank"])


def test_migration_refuses_new_wording_that_is_not_already_confirmed(profile, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    updates = updates_for(profile)
    updates["new_evidence"][0]["public_text"] = "A brand new claim nobody confirmed."
    file = tmp_path / "updates.json"
    file.write_text(json.dumps(updates))
    assert migrate.main(["--profile", str(path), "--updates", str(file), "--apply"]) == 2
    assert json.loads(path.read_text()) == profile
