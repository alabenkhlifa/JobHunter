"""Per-job evidence proposals for unattended (autopilot) application packages."""

import hashlib
import json
import sqlite3

import pytest

import jobhunter_interest_flow as flow
import resume_quality as quality
from test_resume_quality import add_confirmed_payment_evidence, job, profile, reviewed_project_variant, summary  # noqa: F401

PERFORMANCE = "Reduced backend response time with batched database updates and controlled memory use."


def add_performance_evidence(profile):
    profile["evidence_bank"].append({"id": "approved-performance", "experience_id": "role-current",
        "public_text": PERFORMANCE, "confirmation": "candidate-confirmed", "confidentiality": "public",
        "visibility": ["resume"]})
    profile["employment_groups"][0]["engagements"][0]["bullets"].append(PERFORMANCE)


def payments_job(job):
    return dict(job, title="Payments Backend Engineer", description=job["description"] + " Build payments APIs.")


def store(tmp_path, monkeypatch, profile, job):
    db, source, output = tmp_path / "jobs.db", tmp_path / "profile.json", tmp_path / "output"
    source.write_text(json.dumps(profile))
    keys = ("id", "title", "company", "location", "url", "source", "description", "tech_required")
    with sqlite3.connect(db) as connection:
        connection.execute(f"CREATE TABLE jobs ({', '.join(k + ' TEXT' for k in keys)})")
        connection.execute(f"INSERT INTO jobs VALUES ({', '.join('?' * len(keys))})", tuple(job[k] for k in keys))
    monkeypatch.setattr(flow.scraper, "sync_application_tracker_if_enabled", lambda: None)
    return {"db_path": db, "profile_path": source, "output_dir": output}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def planner_with(reviews):
    """A fake owner planner: drafts from the context; reviews pop from ``reviews`` (default: ready)."""
    calls = []

    def planner(messages, schema):
        request = json.loads(messages[-1]["content"])
        calls.append(request)
        if schema == quality.SUMMARY_SCHEMA:
            return summary(request)
        issues = reviews.pop(0) if reviews else []
        return {"verdict": "blocked" if issues else "ready", "blocking_evidence_issues": issues,
                "revised_draft": request["draft"]}
    planner.calls = calls
    return planner


def test_default_path_still_refuses_a_variant_with_evidence_gaps(profile, job, tmp_path, monkeypatch):
    reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    paths = store(tmp_path, monkeypatch, profile, payments_job(job))
    planner = planner_with([])
    with pytest.raises(flow.TailoringReadinessError, match="omits relevant confirmed evidence: payment-monitoring"):
        flow.prepare_application_package(job["id"], summary_planner=planner, **paths)
    assert planner.calls == [] and not paths["output_dir"].exists()


def test_accepted_proposal_builds_the_package_and_records_it(profile, job, tmp_path, monkeypatch):
    variant = reviewed_project_variant(profile)
    text = add_confirmed_payment_evidence(profile)
    paths = store(tmp_path, monkeypatch, profile, payments_job(job))
    before = digest(paths["profile_path"])
    package = flow.prepare_application_package(job["id"], summary_planner=planner_with([]),
                                               accept_evidence_proposals=True, **paths)
    manifest = json.loads(package.manifest_json.read_text())
    assert manifest["evidence_proposal_applied"] is True
    assert [u["evidence_id"] for u in manifest["proposed_evidence_updates"]] == ["payment-monitoring"]
    assert manifest["content_review_revision_rounds"] == 0
    assert manifest["proposed_evidence_omissions"] == ["fact-4"]
    assert manifest["selected_variant_id"] == variant["id"]
    resume = json.loads(package.resume_json.read_text())
    assert text in list(quality._unique_bullets(resume))
    removed_texts = {e["public_text"] for e in profile["evidence_bank"] if e["id"] in manifest["proposed_evidence_omissions"]}
    assert not removed_texts & set(quality._unique_bullets(resume))
    assert digest(paths["profile_path"]) == before
    assert text not in json.loads(paths["profile_path"].read_text())["resume_variants"][0]["resume"]["experience"][0]["bullets"]


def test_reviewer_named_evidence_is_added_in_a_second_round(profile, job, tmp_path, monkeypatch):
    reviewed_project_variant(profile)
    add_performance_evidence(profile)
    paths = store(tmp_path, monkeypatch, profile, job)
    issue = ["Include approved-performance for the optimization responsibility."]
    with pytest.raises(flow.TailoringReadinessError, match="independent evidence review"):
        flow.prepare_application_package(job["id"], summary_planner=planner_with([list(issue)]), **paths)
    before = digest(paths["profile_path"])
    planner = planner_with([list(issue)])
    package = flow.prepare_application_package(job["id"], summary_planner=planner,
                                               accept_evidence_proposals=True, **paths)
    manifest = json.loads(package.manifest_json.read_text())
    assert len(planner.calls) == 4
    assert "approved-performance" in {e["id"] for e in planner.calls[-1]["context"]["evidence"]}
    assert manifest["content_review_revision_rounds"] == 1
    assert manifest["evidence_proposal_applied"] is True
    assert [u["evidence_id"] for u in manifest["proposed_evidence_updates"]] == ["approved-performance"]
    assert digest(paths["profile_path"]) == before


def test_reused_summary_reapplies_the_earlier_reviewer_additions(profile, job, tmp_path, monkeypatch):
    reviewed_project_variant(profile)
    add_performance_evidence(profile)
    paths = store(tmp_path, monkeypatch, profile, job)
    issue = ["Include approved-performance for the optimization responsibility."]
    first = flow.prepare_application_package(job["id"], summary_planner=planner_with([issue]),
                                             accept_evidence_proposals=True, **paths)
    manifest = json.loads(first.manifest_json.read_text())
    draft = {k: v for k, v in manifest["summary_review"].items() if k != "review_source"}
    with pytest.raises(flow.TailoringReadinessError, match="stale"):
        flow.prepare_application_package(job["id"], resume_summary_draft=draft,
                                         accept_evidence_proposals=True, **paths)
    again = flow.prepare_application_package(job["id"], resume_summary_draft=draft, accept_evidence_proposals=True,
                                             prior_evidence_ids=["approved-performance"], **paths)
    assert json.loads(again.manifest_json.read_text())["proposed_evidence_updates"] == manifest["proposed_evidence_updates"]


def test_unfixable_review_failure_hands_off(profile, job, tmp_path, monkeypatch):
    reviewed_project_variant(profile)
    add_confirmed_payment_evidence(profile)
    paths = store(tmp_path, monkeypatch, profile, payments_job(job))
    planner = planner_with([["The summary overstates ownership."]])
    with pytest.raises(flow.TailoringReadinessError, match="overstates ownership"):
        flow.prepare_application_package(job["id"], summary_planner=planner, accept_evidence_proposals=True, **paths)
    assert len(planner.calls) == 2
    assert not list(paths["output_dir"].glob("*"))


def test_review_rounds_are_bounded(profile, job, tmp_path, monkeypatch):
    reviewed_project_variant(profile)
    add_performance_evidence(profile)
    paths = store(tmp_path, monkeypatch, profile, job)
    monkeypatch.setattr(quality, "MAX_CONTENT_REVIEW_ROUNDS", 1)
    planner = planner_with([["Include approved-performance."]])
    with pytest.raises(flow.TailoringReadinessError, match="independent evidence review"):
        flow.prepare_application_package(job["id"], summary_planner=planner, accept_evidence_proposals=True, **paths)
    assert len(planner.calls) == 2


def test_flag_without_gaps_records_no_proposal(profile, job, tmp_path, monkeypatch):
    reviewed_project_variant(profile)
    paths = store(tmp_path, monkeypatch, profile, job)
    package = flow.prepare_application_package(job["id"], summary_planner=planner_with([]),
                                               accept_evidence_proposals=True, **paths)
    manifest = json.loads(package.manifest_json.read_text())
    assert manifest["evidence_proposal_applied"] is False
    assert manifest["proposed_evidence_updates"] == [] and manifest["proposed_evidence_omissions"] == []
    default = flow.prepare_application_package(job["id"], summary_planner=planner_with([]), **paths)
    assert "evidence_proposal_applied" not in json.loads(default.manifest_json.read_text())
