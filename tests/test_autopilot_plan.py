import argparse
import json
import sqlite3
import sys
import types
from contextlib import closing
from pathlib import Path

import pytest

import jobhunter_interest_flow as interest_flow
import scraper
from jobhunter_autopilot import availability, plan, store
from test_autopilot_helpers import (
    NOW, RUN_ID, FakeResearch, add_job, enabled_policy, log_status, make_db, research_payload, result_for,
)


class Sender:
    def __init__(self, ok=True):
        self.ok, self.messages, self.documents = ok, [], []

    def send(self, text, reply_markup=None):
        self.messages.append((text, reply_markup))
        return self.ok

    def document(self, path, caption=None):
        self.documents.append((str(path), caption))
        return self.ok


class Checker:
    def __init__(self, states=None):
        self.states, self.calls = states or {}, []

    def __call__(self, job):
        self.calls.append(job["id"])
        return result_for(job, self.states.get(job["id"], "open"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    conn = make_db(db)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"name": "Ala", "application_defaults": {"notice_period": "1 month"}}))
    monkeypatch.setenv("JOBHUNTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JOBHUNTER_GMAIL_ACCOUNT", "jobs@example.com")
    packaged, researched, answered = [], [], []

    def fake_package(job_id, *, db_path, profile_path, output_dir, **options):
        packaged.append({"job_id": job_id, "db_path": str(db_path), "output_dir": str(output_dir), **options})
        directory = Path(output_dir) / f"{job_id}-pkg"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Resume.pdf").write_text("pdf")
        digest = interest_flow._profile_digest(json.loads(Path(profile_path).read_text()))
        (directory / "tailoring_manifest.json").write_text(json.dumps({"job_id": job_id, "profile_sha256": digest}))
        with closing(sqlite3.connect(db_path)) as other:
            scraper.record_application_stage(other, job_id, "package_generated", package_path=str(directory),
                                             sync=False)
        return interest_flow.ApplicationPackage(job_id, directory, directory / "resume.json", None,
                                                directory / "Resume.pdf", None)

    research_module = types.ModuleType("jobhunter_research")

    def fake_research(job, *, conn=None, now=None, **kwargs):
        researched.append((job["id"], conn is not None))
        return FakeResearch(research_payload(job["id"]))

    research_module.research = fake_research
    answers_module = types.ModuleType("jobhunter_auto_apply.answers")

    def fake_answers(profile, *, research, jobs_mailbox, cached=None):
        answered.append((profile.get("name"), research["job_id"] if research else None, jobs_mailbox))
        return {"email": {"value": jobs_mailbox, "source": "mailbox", "kind": "email"},
                "salary": {"value": str(research["salary"]["ask_amount"]) if research else "",
                           "source": "research", "kind": "number"}}

    answers_module.build_answers = fake_answers
    monkeypatch.setattr(interest_flow, "prepare_application_package", fake_package)
    monkeypatch.setattr(interest_flow, "research_job",
                        lambda job: interest_flow.JobResearch(company_summary="Acme builds things.", legitimacy="ok"))
    monkeypatch.setattr(interest_flow, "build_research_brief_message",
                        lambda job, research: f"🔎 <b>Research</b>\n\n<b>{job['title']}</b>\n{research.company_summary}")
    monkeypatch.setitem(sys.modules, "jobhunter_research", research_module)
    monkeypatch.setitem(sys.modules, "jobhunter_auto_apply.answers", answers_module)
    world = types.SimpleNamespace(conn=conn, db=db, profile=profile, tmp=tmp_path, packaged=packaged,
                                  researched=researched, answered=answered)
    yield world
    conn.close()


def build(world, policy=None, **kwargs):
    kwargs.setdefault("check", Checker())
    kwargs.setdefault("probe", lambda: True)
    kwargs.setdefault("sender", Sender())
    kwargs.setdefault("output_dir", world.tmp / "output")
    return plan.build_plan(world.db, policy or enabled_policy(), now=NOW, profile_path=world.profile, **kwargs)


def test_disabled_or_paused_is_a_noop(world):
    add_job(world.conn)
    disabled = enabled_policy()
    disabled["enabled"] = False
    result, code = build(world, disabled)
    assert code == 0 and result["applications"] == [] and result["enabled"] is False
    assert result["schema"] == "jobhunter.autopilot.plan/1"
    assert not store.table_exists(world.conn, "auto_apply_log")
    result, _ = build(world, enabled_policy(paused=True))
    assert result["paused"] is True and result["applications"] == [] and world.packaged == []


def test_plans_open_jobs_up_to_the_cap(world):
    jobs = [add_job(world.conn, score=90 - i, tech_required="golang") for i in range(4)]
    result, code = build(world, enabled_policy(max_per_day=2))
    assert code == 0
    assert [a["job_id"] for a in result["applications"]] == [jobs[0]["id"], jobs[1]["id"]]
    entry = result["applications"][0]
    assert set(entry) >= {"job_id", "title", "company", "location", "listing_url", "apply_url", "platform_hint",
                          "score", "score_basis", "verdict", "criteria", "availability", "package", "research",
                          "answers"}
    assert entry["platform_hint"] == "linkedin" and entry["apply_url"] is None
    assert entry["criteria"][:2] == ["score 90 ≥ 75", "reviewer: send"]
    assert entry["availability"]["state"] == "open"
    assert entry["package"]["resume_pdf"].endswith("Resume.pdf")
    assert entry["answers"]["salary"]["value"] == "62000"
    assert entry["research"]["salary"]["ask_amount"] == 62000
    assert result["cap"] == {"max_per_day": 2, "used_today": 0, "remaining": 2}
    assert result["browser"] == {"cdp_port": 9333, "ok": True}
    assert result["preflight"]["missing_application_defaults"] == [
        "citizenship", "current_work_location", "requires_visa_sponsorship", "allow_contact_for_other_vacancies"]
    assert result["preflight"]["jobs_mailbox"] == "jobs@example.com"
    for job in jobs[:2]:
        assert log_status(world.conn, job["id"]) == "packaged"
        status = world.conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
        assert status == "interested"
    assert world.conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 0
    assert log_status(world.conn, jobs[2]["id"]) is None
    assert store.get_run(world.conn, RUN_ID)["plan_finished_at"]
    assert all(conn_used for _, conn_used in world.researched)


def test_closed_unknown_and_archived_closures(world):
    archived = add_job(world.conn, status="archived", score=95)
    unknown = add_job(world.conn, score=94)
    good = add_job(world.conn, score=93)
    checker = Checker({archived["id"]: "closed", unknown["id"]: "unknown"})
    result, _ = build(world, check=checker)
    assert [a["job_id"] for a in result["applications"]] == [good["id"]]
    assert result["closed"] == [{"job_id": archived["id"], "title": archived["title"],
                                 "company": archived["company"], "tracked": False, "sheet_synced": False}]
    assert [d["job_id"] for d in result["deferred"]] == [unknown["id"]]
    assert world.conn.execute("SELECT status FROM jobs WHERE id=?", (archived["id"],)).fetchone()[0] == "unavailable"
    assert world.conn.execute("SELECT status FROM jobs WHERE id=?", (unknown["id"],)).fetchone()[0] == "new"
    assert log_status(world.conn, archived["id"]) == "closed"
    assert log_status(world.conn, unknown["id"]) == "deferred"
    saved = world.conn.execute("SELECT state FROM job_availability WHERE job_id=?", (archived["id"],)).fetchone()
    assert saved[0] == "closed"


def test_tracked_closure_goes_through_the_listing_monitor_with_the_cached_check(world, monkeypatch):
    tracked = add_job(world.conn, score=95)
    scraper.record_application_stage(world.conn, tracked["id"], "interested", sync=False)
    checker = Checker({tracked["id"]: "closed"})
    calls = []

    def fake_monitor(args, send, *, check):
        calls.append(args.db_path)
        again = check({"id": tracked["id"], "url": tracked["url"], "source": "LinkedIn"})
        assert again["state"] == "closed"
        with closing(sqlite3.connect(args.db_path)) as other:
            other.execute("CREATE TABLE IF NOT EXISTS tracker_listing_closures (row_key TEXT PRIMARY KEY, job_id TEXT,"
                          " closed_at TEXT, tracker_synced_at TEXT)")
            other.execute("INSERT INTO tracker_listing_closures VALUES ('k', ?, 'now', 'now')", (tracked["id"],))
            other.commit()
        send("closed alert")
        return {"checked": 1}

    monkeypatch.setattr(availability, "_tracker_args", lambda db: argparse.Namespace(db_path=Path(db)))
    sender = Sender()
    result, _ = build(world, check=checker, monitor=fake_monitor, sender=sender)
    assert calls == [world.db]
    assert checker.calls == [tracked["id"]]
    assert result["closed"][0]["tracked"] is True and result["closed"][0]["sheet_synced"] is True
    assert sender.messages[0][0] == "closed alert"


def test_replan_is_idempotent_and_respects_the_cap(world):
    jobs = [add_job(world.conn, score=90 - i, tech_required="golang") for i in range(4)]
    first, _ = build(world, enabled_policy(max_per_day=2))
    calls = len(world.packaged)
    second, _ = build(world, enabled_policy(max_per_day=2))
    assert [a["job_id"] for a in second["applications"]] == [a["job_id"] for a in first["applications"]]
    assert len(world.packaged) == calls
    assert second["counts"]["carried_over"] == 2
    store.upsert_log(world.conn, RUN_ID, jobs[0]["id"], "submitted")
    store.upsert_log(world.conn, RUN_ID, jobs[1]["id"], "blocked")
    third, _ = build(world, enabled_policy(max_per_day=2))
    # One slot is used by the submission; the blocked one freed a slot for the next job.
    assert [a["job_id"] for a in third["applications"]] == [jobs[2]["id"]]
    assert third["cap"] == {"max_per_day": 2, "used_today": 1, "remaining": 1}


def test_rehearsals_use_the_days_slots_in_rehearsal_mode(world):
    jobs = [add_job(world.conn, score=90 - i) for i in range(3)]
    store.ensure_schema(world.conn)
    store.upsert_log(world.conn, RUN_ID, jobs[0]["id"], "rehearsed")
    result, _ = build(world, enabled_policy(max_per_day=1))
    assert result["applications"] == [] and result["cap"]["remaining"] == 0


def test_package_failure_hands_off_with_the_research_card(world, monkeypatch):
    bad = add_job(world.conn, score=95)
    good = add_job(world.conn, score=90)

    def failing(job_id, **kwargs):
        if job_id == bad["id"]:
            raise interest_flow.TailoringReadinessError("Languages are required in every resume.")
        return original(job_id, **kwargs)

    original = interest_flow.prepare_application_package
    monkeypatch.setattr(interest_flow, "prepare_application_package", failing)
    sender = Sender()
    result, _ = build(world, sender=sender)
    assert [a["job_id"] for a in result["applications"]] == [good["id"]]
    handoff = result["handoffs"][0]
    assert handoff["job_id"] == bad["id"] and handoff["telegram"] == "sent"
    assert handoff["reason"].startswith("package_not_ready: Languages are required")
    text, keyboard = sender.messages[0]
    assert "Autopilot handed this offer to you" in text and "🔎 <b>Research</b>" in text
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == f"apply:{bad['id']}"
    stage, error = world.conn.execute("SELECT stage, error FROM applications WHERE job_id=?", (bad["id"],)).fetchone()
    assert stage == "blocked_auto_apply" and error.startswith("package_not_ready: ")
    feedback = world.conn.execute("SELECT action, source FROM job_feedback WHERE job_id=?", (bad["id"],)).fetchall()
    assert feedback == [("research_delivered", "telegram_research_card")]
    assert log_status(world.conn, bad["id"]) == "handed_off"


def test_dry_run_reads_only_and_packages_to_a_copy(world):
    job = add_job(world.conn, status="archived")
    before = world.db.read_bytes()
    sender = Sender()
    result, code = build(world, dry_run=True, sender=sender)
    assert code == 0 and result["dry_run"] is True
    assert [a["job_id"] for a in result["applications"]] == [job["id"]]
    assert world.db.read_bytes() == before
    assert not store.table_exists(world.conn, "auto_apply_log")
    assert sender.messages == []
    call = world.packaged[0]
    assert call["db_path"] != str(world.db) and not call["output_dir"].startswith(str(world.tmp / "output"))
    assert result["dry_run_output"] == call["output_dir"]
    assert world.researched == [(job["id"], False)]
    assert world.conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == "archived"


def test_dry_run_works_while_disabled_and_reports_filters(world):
    add_job(world.conn)
    add_job(world.conn, location="Dubai, United Arab Emirates")
    add_job(world.conn, ai_verdict="")
    disabled = enabled_policy()
    disabled["enabled"] = False
    result, _ = build(world, disabled, dry_run=True, no_package=True, no_research=True)
    assert len(result["applications"]) == 1 and result["applications"][0]["package"] is None
    assert result["counts"]["filtered"]["country"] == 1 and result["counts"]["needs_review"] == 1


def test_locked_run_exits_pending(world):
    add_job(world.conn)
    with store.autopilot_lock():
        result, code = build(world)
    assert code == 3 and result["locked"] is True and result["applications"] == []


def test_existing_package_from_the_current_profile_is_reused(world):
    job = add_job(world.conn)
    directory = world.tmp / "output" / "pkg"
    directory.mkdir(parents=True)
    (directory / "Resume.pdf").write_text("pdf")
    profile = json.loads(world.profile.read_text())
    (directory / "tailoring_manifest.json").write_text(json.dumps(
        {"job_id": job["id"], "profile_sha256": interest_flow._profile_digest(profile)}))
    scraper.record_application_stage(world.conn, job["id"], "package_generated", package_path=str(directory),
                                     sync=False)
    result, _ = build(world)
    assert result["applications"][0]["package"]["dir"] == str(directory)
    assert world.packaged == []


def test_browser_probe():
    class Response:
        status_code = 200

        def json(self):
            return {"Browser": "Chrome"}

    assert plan.probe_browser(get=lambda url, timeout: Response())

    def down(url, timeout):
        raise OSError("refused")

    assert not plan.probe_browser(get=down)


@pytest.mark.parametrize("url, hint", [
    ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://acme.wd3.myworkdayjobs.com/x", "workday"),
    ("https://jobs.lever.co/acme/1", "lever"),
    ("https://careers.acme.es/apply", "careers.acme.es"),
    ("", "unknown"),
])
def test_platform_hint(url, hint):
    assert plan.platform_hint(url) == hint


def test_research_is_kept_for_the_report_when_the_shared_cache_skips_it(world):
    job = add_job(world.conn)
    build(world)
    assert plan.cached_research(world.conn, job["id"])["salary"]["ask_amount"] == 62000
    path = plan._research_file(job["id"])
    assert path.parent == world.tmp / "state" / "autopilot-research"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_browser_down_or_missing_profile_plans_nothing(world, tmp_path):
    add_job(world.conn)
    result, code = build(world, probe=lambda: False)
    assert code == 0 and result["blocked_reason"] == "browser_unavailable" and result["applications"] == []
    result, _ = plan.build_plan(world.db, enabled_policy(), now=NOW, profile_path=tmp_path / "missing.json",
                                check=Checker(), probe=lambda: True, sender=Sender())
    assert result["blocked_reason"] == "package_not_ready" and world.packaged == []
    assert not store.table_exists(world.conn, "auto_apply_log")


def test_repeated_package_failures_stop_the_run(world, monkeypatch):
    jobs = [add_job(world.conn, score=95 - i) for i in range(4)]

    def failing(job_id, **kwargs):
        raise interest_flow.TailoringReadinessError("Summary drafting or review is unavailable")

    monkeypatch.setattr(interest_flow, "prepare_application_package", failing)
    result, _ = build(world, enabled_policy(max_per_day=3))
    assert [h["job_id"] for h in result["handoffs"]] == [jobs[0]["id"], jobs[1]["id"]]
    assert result["applications"] == [] and "planning stopped" in " ".join(result["notes"])
    assert log_status(world.conn, jobs[2]["id"]) is None


def test_render_errors_defer_without_blocking(world, monkeypatch):
    job = add_job(world.conn)
    add_job(world.conn, score=80)

    def broken(job_id, **kwargs):
        raise RuntimeError("chromium crashed")

    monkeypatch.setattr(interest_flow, "prepare_application_package", broken)
    result, _ = build(world)
    assert result["deferred"][0]["job_id"] == job["id"] and result["handoffs"] == []
    assert log_status(world.conn, job["id"]) == "deferred"
    assert world.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
