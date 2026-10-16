import io
import json
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

import jobhunter_interest_flow as interest_flow
import scraper
from jobhunter_autopilot import cli, plan, policy, store
from test_autopilot_helpers import (
    NOW, RUN_ID, add_job, enabled_policy, log_status, make_db, research_payload, telegram_ok, write_policy,
)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    conn = make_db(db)
    store.ensure_schema(conn)
    policy_path = write_policy(tmp_path / "policy.json", enabled_policy(mode="live"))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"name": "Ala Example"}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("JOBHUNTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setitem(sys.modules, "jobhunter_research", None)
    monkeypatch.setattr(interest_flow, "research_job",
                        lambda job: interest_flow.JobResearch(company_summary="Acme builds things.", legitimacy="ok"))
    monkeypatch.setattr(interest_flow, "build_research_brief_message",
                        lambda job, research: f"🔎 <b>Research</b>\n\n<b>{job['title']}</b>\nChoose next step:")
    base = ["--db", str(db), "--policy", str(policy_path), "--profile", str(profile)]
    yield conn, db, policy_path, profile, base, tmp_path
    conn.close()


def run(capsys, argv, *, now=NOW, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(argv, now=now)
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


def sent(post, kind="sendMessage"):
    return [call for call in post.call_args_list if call.args[0].endswith(kind)]


def receipt(tmp_path, job_id, *, checked_at=NOW, verified=True, schema="jobhunter.receipt/1", evidence=True):
    shot = tmp_path / f"{job_id}.png"
    if evidence:
        shot.write_bytes(b"png")
    path = tmp_path / f"{job_id}-receipt.json"
    path.write_text(json.dumps({
        "schema": schema, "job_id": job_id, "page_url": "https://boards.greenhouse.io/acme/jobs/1",
        "final_url": "https://boards.greenhouse.io/acme/jobs/1/confirmation", "checked_at": checked_at.isoformat(),
        "verified": verified, "markers": ["Thank you for applying"], "title_match": True, "company_match": True,
        "evidence_path": str(shot), "sha256": "0" * 64}))
    return path


# ── config ───────────────────────────────────────────────────────────────────

def test_config_round_trip(env, capsys):
    _, _, policy_path, _, base, tmp = env
    fresh = tmp / "fresh.json"
    args = ["--policy", str(fresh)]
    code, out = run(capsys, args + ["config", "show"])
    assert code == 0 and out.startswith("Auto apply: OFF (rehearsal")
    code, out = run(capsys, args + ["--via", "telegram", "config", "set", "min_score=80", "max_per_day=1"])
    assert code == 0 and "Min score 80 · max 1/day" in out
    assert stat.S_IMODE(os.stat(fresh).st_mode) == 0o600
    assert policy.load(fresh)["updated_via"] == "telegram"
    assert run(capsys, args + ["config", "enable"])[1].startswith("Auto apply: ON (rehearsal")
    assert "ON (live" in run(capsys, args + ["config", "live"])[1]
    assert run(capsys, args + ["config", "rehearsal"])[0] == 0
    code, out = run(capsys, args + ["config", "pause", "--until", "2d"])
    assert code == 0 and out.startswith("Auto apply: PAUSED until")
    assert run(capsys, args + ["config", "resume"])[1].startswith("Auto apply: ON")
    assert run(capsys, args + ["config", "disable"])[1].startswith("Auto apply: OFF")
    code, out = run(capsys, args + ["config", "show", "--json"])
    assert out["min_score"] == 80 and out["enabled"] is False
    assert run(capsys, args + ["config", "reset"])[0] == 0
    assert policy.load(fresh)["min_score"] == 75


def test_config_errors_exit_2_without_writing(env, capsys):
    tmp = env[5]
    path = tmp / "p.json"
    for bad in (["config", "set", "min_score=500"], ["config", "set", "mode=yolo"],
                ["config", "set", "roles=ceo"], ["config", "pause", "--until", "someday"]):
        code, out = run(capsys, ["--policy", str(path)] + bad)
        assert code == 2 and out["ok"] is False and out["error"]
    assert not path.exists()
    assert run(capsys, ["config", "nonsense"])[0] == 2


def test_config_defaults_set_writes_application_defaults(env, capsys):
    profile, base = env[3], env[4]
    code, out = run(capsys, base + ["config", "defaults", "set", "notice_period=1 month",
                                    "requires_visa_sponsorship=yes", "citizenship=Tunisian"])
    assert code == 0 and out["updated"] == ["citizenship", "notice_period", "requires_visa_sponsorship"]
    stored = json.loads(profile.read_text())
    assert stored["application_defaults"] == {"notice_period": "1 month", "requires_visa_sponsorship": True,
                                              "citizenship": "Tunisian"}
    assert stored["name"] == "Ala Example"
    assert Path(out["backup"]).exists()
    assert out["missing_application_defaults"] == ["current_work_location", "allow_contact_for_other_vacancies"]
    run(capsys, base + ["config", "defaults", "set", "allow_contact_for_other_vacancies=no"])
    stored = json.loads(profile.read_text())
    assert stored["application_defaults"]["allow_contact_for_other_vacancies"] is False
    assert stored["application_defaults"]["notice_period"] == "1 month"
    assert run(capsys, base + ["config", "defaults", "set", "requires_visa_sponsorship=maybe"])[0] == 2
    assert run(capsys, base + ["config", "defaults", "set", "salary=100"])[0] == 2


# ── review ───────────────────────────────────────────────────────────────────

def test_review_batch_and_record_review(env, capsys, monkeypatch):
    conn, base = env[0], env[4]
    old = add_job(conn, status="archived", ai_verdict="", date_posted="2026-05-01",
                  date_scraped="2026-05-01T20:00:00+00:00")
    code, out = run(capsys, base + ["review-batch"])
    assert code == 0 and out["count"] == 1 and out["autopilot"]["needs_review"] == {old["id"]: "unreviewed"}
    verdicts = json.dumps([{"job_id": old["id"], "verdict": "send", "reason": "fit", "sponsorship": "no_info",
                            "rank": 1}])
    code, out = run(capsys, base + ["record-review"], stdin=verdicts, monkeypatch=monkeypatch)
    assert code == 0 and out["recorded"] == 1 and out["send"] == 1 and out["review_notes"] == []
    assert conn.execute("SELECT ai_verdict FROM jobs WHERE id=?", (old["id"],)).fetchone()[0] == "send"
    assert store.get_run(conn, RUN_ID)["review_finished_at"]
    assert run(capsys, base + ["record-review"], stdin="{oops", monkeypatch=monkeypatch)[0] == 2
    assert run(capsys, base + ["record-review"], stdin='{"a": 1}', monkeypatch=monkeypatch)[0] == 2


def test_missing_database_is_invalid_input(env, capsys, tmp_path):
    code, out = run(capsys, ["--db", str(tmp_path / "none.db"), "--policy", str(env[2]), "review-batch"])
    assert code == 2 and "database not found" in out["error"]


# ── plan / package ───────────────────────────────────────────────────────────

def test_plan_command_passes_options_and_exit_code(env, capsys, monkeypatch):
    seen = {}

    def fake_build(db_path, current, **kwargs):
        seen.update(kwargs, db_path=db_path)
        return {"schema": plan.SCHEMA}, 3

    monkeypatch.setattr(plan, "build_plan", fake_build)
    code, out = run(capsys, env[4] + ["plan", "--dry-run", "--no-package", "--no-research"])
    assert code == 3 and out == {"schema": plan.SCHEMA}
    assert seen["dry_run"] and seen["no_package"] and seen["no_research"] and seen["sender"] is None
    run(capsys, env[4] + ["--no-telegram", "plan"])
    assert seen["sender"] is None and seen["dry_run"] is False
    run(capsys, env[4] + ["plan"])
    assert seen["sender"] is not None


def test_package_requires_todays_plan_and_reuses_summary(env, capsys, monkeypatch):
    conn, base, tmp = env[0], env[4], env[5]
    job = add_job(conn)
    assert run(capsys, base + ["package", job["id"]])[0] == 4
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    directory = tmp / "pkg"
    directory.mkdir()
    (directory / "tailoring_manifest.json").write_text(json.dumps({"summary_review": {
        "text": "S.", "evidence_ids": ["e1"], "context_sha256": "abc", "review_flags": [],
        "review_source": "independent_model"}, "evidence_proposal_applied": True,
        "proposed_evidence_updates": [{"evidence_id": "ev-added"}], "proposed_evidence_omissions": [],
        "content_review_revision_rounds": 1}))
    scraper.record_application_stage(conn, job["id"], "package_generated", package_path=str(directory), sync=False)
    draft = tmp / "cover.json"
    draft.write_text(json.dumps({"paragraphs": ["Hello"]}))
    calls = []

    def fake_package(job_id, **kwargs):
        calls.append(kwargs)
        return interest_flow.ApplicationPackage(job_id, directory, directory / "r.json", directory / "c.json",
                                                directory / "Resume.pdf", directory / "CoverLetter.pdf")

    monkeypatch.setattr(interest_flow, "prepare_application_package", fake_package)
    code, out = run(capsys, base + ["package", job["id"], "--cover-draft", str(draft), "--reuse-summary"])
    assert code == 0 and out["package"]["cover_pdf"].endswith("CoverLetter.pdf")
    assert calls[0]["include_cover_letter"] is True and calls[0]["cover_letter_draft"] == {"paragraphs": ["Hello"]}
    assert calls[0]["resume_summary_draft"] == {"text": "S.", "evidence_ids": ["e1"], "context_sha256": "abc",
                                                "review_flags": []}
    assert calls[0]["accept_evidence_proposals"] is True and calls[0]["prior_evidence_ids"] == ["ev-added"]
    assert out["package"]["evidence_proposal"] == {"added": ["ev-added"], "removed": [], "rounds": 1}

    def failing(job_id, **kwargs):
        raise interest_flow.TailoringReadinessError("Cover letter draft is stale")

    monkeypatch.setattr(interest_flow, "prepare_application_package", failing)
    code, out = run(capsys, base + ["package", job["id"], "--cover-draft", str(draft)])
    assert code == 2 and out["hard_stop"] == "cover_letter_failed"


def test_answers_command(env, capsys, monkeypatch):
    import types
    conn, base = env[0], env[4]
    job = add_job(conn)
    module = types.ModuleType("jobhunter_auto_apply.answers")
    module.build_answers = lambda profile, *, research, jobs_mailbox, cached=None: {
        "name": {"value": profile["name"], "source": "profile", "kind": "text"}}
    monkeypatch.setitem(sys.modules, "jobhunter_auto_apply.answers", module)
    code, out = run(capsys, base + ["answers", job["id"]])
    assert code == 0 and out["answers"]["name"]["value"] == "Ala Example"
    monkeypatch.setitem(sys.modules, "jobhunter_auto_apply.answers", None)
    assert run(capsys, base + ["answers", job["id"]])[0] == 1


# ── record-submitted ─────────────────────────────────────────────────────────

def test_record_submitted_records_stage_and_reports_once(env, capsys):
    conn, base, tmp = env[0], env[4], env[5]
    job = add_job(conn, status="interested")
    store.upsert_log(conn, RUN_ID, job["id"], "applying", criteria=["score 88 ≥ 75", "reviewer: send"])
    conn.execute("CREATE TABLE job_research (job_id TEXT PRIMARY KEY, payload_json TEXT)")
    conn.execute("INSERT INTO job_research VALUES (?, ?)", (job["id"], json.dumps(research_payload(job["id"]))))
    conn.commit()
    path = receipt(tmp, job["id"], checked_at=NOW - timedelta(minutes=5))
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(path)])
        assert code == 0 and out["telegram"] == "sent"
        messages = sent(post)
        assert len(messages) == 1 and len(sent(post, "sendDocument")) == 1
        text = messages[0].kwargs["json"]["text"]
        assert text.startswith("✅ Auto-applied (live)") and "score 88 ≥ 75 · reviewer: send" in text
        assert "Asked: €62,000" in text and "Evidence: screenshot attached" in text
        code, out = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(path)])
        assert code == 0 and out["telegram"] == "already_sent" and len(sent(post)) == 1
    row = conn.execute("SELECT stage, submitted_at, evidence_path, application_url, notes FROM applications "
                       "WHERE job_id=?", (job["id"],)).fetchone()
    assert row[0] == "submitted" and row[1] and row[2].endswith(".png")
    assert row[3].endswith("/confirmation") and row[4] == f"autopilot {RUN_ID}"
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "submitted"
    assert log_status(conn, job["id"]) == "submitted"
    assert store.cap_used_today(conn, RUN_ID) == 1


@pytest.mark.parametrize("kwargs, problem", [
    ({"checked_at": NOW - timedelta(minutes=31)}, "older than 30 minutes"),
    ({"verified": False}, "not verified"),
    ({"schema": "other/1"}, "schema"),
    ({"checked_at": NOW + timedelta(minutes=10)}, "future"),
])
def test_record_submitted_rejects_bad_receipts(env, capsys, kwargs, problem):
    conn, base, tmp = env[0], env[4], env[5]
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(receipt(tmp, job["id"], **kwargs))])
    assert code == 3 and problem in out["error"] and post.call_count == 0
    assert log_status(conn, job["id"]) == "unverified"
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE job_id=?", (job["id"],)).fetchone()[0] == 0


def test_record_submitted_message_lists_the_packages_evidence_proposal(env, capsys):
    conn, base, tmp = env[0], env[4], env[5]
    job = add_job(conn, status="interested")
    directory = tmp / "pkg"
    directory.mkdir()
    (directory / "tailoring_manifest.json").write_text(json.dumps({
        "job_id": job["id"], "evidence_proposal_applied": True,
        "proposed_evidence_updates": [{"evidence_id": "ev-a"}, {"evidence_id": "ev-b"}],
        "proposed_evidence_omissions": ["ev-c"], "content_review_revision_rounds": 0}))
    scraper.record_application_stage(conn, job["id"], "package_generated", package_path=str(directory), sync=False)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, _ = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(receipt(tmp, job["id"]))])
        assert code == 0
        text = sent(post)[0].kwargs["json"]["text"]
    assert "Resume: +ev-a, +ev-b · −ev-c (per-job evidence proposal)" in text
    assert cli.run_stats(conn, enabled_policy(mode="live"), RUN_ID)["evidence_proposals"] == 1


def test_record_submitted_rejects_another_jobs_receipt_and_unplanned_jobs(env, capsys):
    conn, base, tmp = env[0], env[4], env[5]
    job, other = add_job(conn), add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    code, out = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(receipt(tmp, other["id"]))])
    assert code == 3 and "another job" in out["error"]
    code, out = run(capsys, base + ["record-submitted", other["id"], "--receipt", str(receipt(tmp, other["id"]))])
    assert code == 2 and "not in today's" in out["error"]
    assert run(capsys, base + ["record-submitted", "nope", "--receipt", "x"])[0] == 2


def test_record_submitted_telegram_failure_keeps_state_and_retries(env, capsys, monkeypatch):
    conn, base, tmp = env[0], env[4], env[5]
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    path = receipt(tmp, job["id"], evidence=False)
    failed = mock.Mock(status_code=500)
    with mock.patch.object(scraper.requests, "post", return_value=failed):
        code, out = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(path)])
    assert code == 5 and out["status"] == "submitted"
    assert log_status(conn, job["id"]) == "submitted"
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["record-submitted", job["id"], "--receipt", str(path)])
        assert code == 0 and len(sent(post)) == 1 and sent(post, "sendDocument") == []
        assert "Evidence: none attached" in sent(post)[0].kwargs["json"]["text"]
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE job_id=?", (job["id"],)).fetchone()[0] == 1
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    other = add_job(conn)
    store.upsert_log(conn, RUN_ID, other["id"], "applying")
    code, _ = run(capsys, base + ["record-submitted", other["id"], "--receipt", str(receipt(tmp, other["id"]))])
    assert code == 5 and log_status(conn, other["id"]) == "submitted"
    third = add_job(conn)
    store.upsert_log(conn, RUN_ID, third["id"], "applying")
    code, out = run(capsys, base + ["--no-telegram", "record-submitted", third["id"], "--receipt",
                                    str(receipt(tmp, third["id"]))])
    assert code == 0 and out["telegram"] == "skipped"


def test_rehearsed_reports_without_touching_the_tracker(env, capsys):
    conn, base, tmp = env[0], env[4], env[5]
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    shot = tmp / "filled.png"
    shot.write_bytes(b"png")
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["rehearsed", job["id"], "--evidence", str(shot)])
        assert code == 0 and out["status"] == "rehearsed"
        assert sent(post)[0].kwargs["json"]["text"].startswith("🧪 Rehearsed (not submitted)")
        assert len(sent(post, "sendDocument")) == 1
        run(capsys, base + ["rehearsed", job["id"]])
        assert len(sent(post)) == 1
    assert log_status(conn, job["id"]) == "rehearsed"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


# ── blocked ──────────────────────────────────────────────────────────────────

def test_blocked_records_stage_sends_card_once_and_records_feedback(env, capsys):
    conn, base = env[0], env[4]
    job = add_job(conn, status="interested")
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["blocked", job["id"], "--reason", "captcha", "--detail", "hCaptcha on submit"])
        assert code == 0 and out["status"] == "handed_off" and out["telegram"] == "sent"
        payload = sent(post)[0].kwargs["json"]
        assert "the site showed a CAPTCHA — hCaptcha on submit" in payload["text"]
        assert "🔎 <b>Research</b>" in payload["text"]
        buttons = payload["reply_markup"]["inline_keyboard"][0]
        assert [b.get("callback_data") for b in buttons[:2]] == [f"apply:{job['id']}", f"ignore:{job['id']}"]
        assert buttons[2]["url"] == job["url"]
        code, out = run(capsys, base + ["blocked", job["id"], "--reason", "captcha"])
        assert code == 0 and out["telegram"] == "already_sent" and len(sent(post)) == 1
    stage, error = conn.execute("SELECT stage, error FROM applications WHERE job_id=?", (job["id"],)).fetchone()
    assert stage == "blocked_auto_apply" and error == "captcha"
    actions = [r[0] for r in conn.execute("SELECT action FROM job_feedback WHERE job_id=?", (job["id"],))]
    assert actions == ["research_delivered"]
    assert log_status(conn, job["id"]) == "handed_off"


def test_blocked_after_submit_keeps_the_stage(env, capsys):
    conn, base = env[0], env[4]
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    scraper.record_application_stage(conn, job["id"], "submission_attempted", sync=False)
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, _ = run(capsys, base + ["blocked", job["id"], "--reason", "receipt_unverified"])
        assert "already clicked" in sent(post)[0].kwargs["json"]["text"]
    assert code == 0
    assert conn.execute("SELECT stage FROM applications WHERE job_id=?", (job["id"],)).fetchone()[0] == \
        "submission_attempted"


def test_blocked_hand_off_cap_and_validation(env, capsys, tmp_path):
    conn, db, _, profile, _, tmp = env
    policy_path = write_policy(tmp / "one.json", enabled_policy(mode="live", max_handoffs_per_run=1))
    base = ["--db", str(db), "--policy", str(policy_path), "--profile", str(profile)]
    first, second = add_job(conn), add_job(conn)
    for job in (first, second):
        store.upsert_log(conn, RUN_ID, job["id"], "applying")
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        assert run(capsys, base + ["blocked", first["id"], "--reason", "unanswerable:Notice period"])[0] == 0
        code, out = run(capsys, base + ["blocked", second["id"], "--reason", "budget_exceeded"])
        assert code == 0 and out["telegram"] == "cap" and len(sent(post)) == 1
    assert log_status(conn, second["id"]) == "blocked"
    assert run(capsys, base + ["blocked", first["id"], "--reason", "bogus"])[0] == 2
    unplanned = add_job(conn)
    assert run(capsys, base + ["blocked", unplanned["id"], "--reason", "captcha"])[0] == 2


def test_blocked_telegram_unconfirmed_exits_5_with_state_saved(env, capsys, monkeypatch):
    conn, base = env[0], env[4]
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "applying")
    with mock.patch.object(scraper.requests, "post", return_value=mock.Mock(status_code=500)):
        code, out = run(capsys, base + ["blocked", job["id"], "--reason", "captcha"])
    assert code == 5 and out["telegram"] == "failed" and log_status(conn, job["id"]) == "blocked"
    assert conn.execute("SELECT stage FROM applications WHERE job_id=?", (job["id"],)).fetchone()[0] == \
        "blocked_auto_apply"
    assert conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 0
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()):
        code, out = run(capsys, base + ["blocked", job["id"], "--reason", "captcha"])
    assert code == 0 and out["telegram"] == "sent"


def test_research_card_falls_back_to_a_minimal_card(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(interest_flow, "research_job", broken)
    monkeypatch.setattr(interest_flow, "build_default_research", broken)
    card = plan.research_card({"id": "x", "title": "<T>", "company": "C", "location": "Madrid"})
    assert card.startswith("🔎 <b>Research</b>") and "&lt;T&gt;" in card


# ── summary / status ─────────────────────────────────────────────────────────

def test_summary_is_sent_once_per_run(env, capsys):
    conn, base = env[0], env[4]
    done, handed, gone = add_job(conn), add_job(conn), add_job(conn, title="Senior Java Developer")
    store.upsert_log(conn, RUN_ID, done["id"], "submitted")
    store.upsert_log(conn, RUN_ID, handed["id"], "handed_off", reason="captcha: hCaptcha")
    store.upsert_log(conn, RUN_ID, gone["id"], "closed")
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["summary"])
        assert code == 0 and out["sent"] is True
        text = sent(post)[0].kwargs["json"]["text"]
        assert "Applied: 1/2" in text and "the site showed a CAPTCHA" in text and "Closed offers: 1" in text
        assert "Cap left today: 2/3" in text
        code, out = run(capsys, base + ["summary"])
        assert code == 0 and out["sent"] is False and out["already_sent_at"] and len(sent(post)) == 1
        run(capsys, base + ["summary", "--force"])
        assert len(sent(post)) == 2
    assert json.loads(store.get_run(conn, RUN_ID)["summary_json"])["applied"] == 1


def test_summary_quiet_day_and_telegram_failure(env, capsys):
    conn, base = env[0], env[4]
    with mock.patch.object(scraper.requests, "post", return_value=telegram_ok()) as post:
        code, out = run(capsys, base + ["summary"])
    assert code == 0 and out["reason"] == "nothing to report" and post.call_count == 0
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "deferred")
    with mock.patch.object(scraper.requests, "post", return_value=mock.Mock(status_code=500)):
        assert run(capsys, base + ["summary"])[0] == 5
    assert store.get_run(conn, RUN_ID)["summary_sent_at"] is None
    code, out = run(capsys, base + ["--no-telegram", "summary"])
    assert code == 0 and "Deferred to the next run: 1" in out["message"]


def test_status(env, capsys):
    conn, base = env[0], env[4]
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    code, out = run(capsys, base + ["status"])
    assert code == 0 and out["run_id"] == RUN_ID and out["active"] is True
    assert out["log"][0]["status"] == "packaged" and out["stats"]["not_attempted"][0]["job_id"] == job["id"]


def test_python_dash_m_entry_point(tmp_path):
    env = dict(os.environ, JOBHUNTER_AUTOPILOT_POLICY=str(tmp_path / "p.json"))
    completed = subprocess.run([sys.executable, "-m", "jobhunter_autopilot", "config", "show"], cwd=REPO,
                               capture_output=True, text=True, env=env, timeout=60)
    assert completed.returncode == 0 and completed.stdout.startswith("Auto apply: OFF")


def test_global_options_work_after_the_command(env, capsys):
    tmp = env[5]
    path = tmp / "late.json"
    code, out = run(capsys, ["config", "set", "min_score=81", "--policy", str(path), "--via", "telegram"])
    assert code == 0 and policy.load(path)["min_score"] == 81 and policy.load(path)["updated_via"] == "telegram"
    code, out = run(capsys, ["--policy", str(path), "config", "show", "--json"])
    assert out["min_score"] == 81
