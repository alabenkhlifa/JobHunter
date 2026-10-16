"""Cron B pre-run: everything deterministic before the agent opens a browser.

lock → selection → availability → research → package → answers → cap →
log rows → plan JSON. A retry the same day carries today's ``packaged`` and
``applying`` rows forward, retries ``selected``/``deferred`` ones and never
plans beyond the remaining cap. ``dry_run`` reads the database read-only,
sends nothing, writes no log rows and packages into a temporary copy.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

import jobhunter_interest_flow as interest_flow
import scraper

from . import availability, codes, messages, selection, store
from . import policy as policy_module

SCHEMA = "jobhunter.autopilot.plan/1"
CDP_PORT = 9333
BROWSER_URL = f"http://127.0.0.1:{CDP_PORT}/json/version"
REPO = Path(__file__).resolve().parent.parent
REQUIRED_DEFAULTS = ("notice_period", "citizenship", "current_work_location",
                     "requires_visa_sponsorship", "allow_contact_for_other_vacancies")
MAX_PACKAGE_FAILURES = 2
RESEARCH_DELIVERED_ACTION = "research_delivered"  # callback_handler.RESEARCH_DELIVERED_ACTION
RESEARCH_CARD_SOURCE = "telegram_research_card"
PLATFORM_HOSTS = (
    ("linkedin.com", "linkedin"), ("greenhouse.io", "greenhouse"), ("lever.co", "lever"),
    ("myworkdayjobs.com", "workday"), ("workday.com", "workday"), ("smartrecruiters.com", "smartrecruiters"),
    ("teamtailor.com", "teamtailor"), ("ashbyhq.com", "ashby"), ("recruitee.com", "recruitee"),
    ("personio.", "personio"), ("bamboohr.com", "bamboohr"), ("successfactors", "successfactors"),
    ("taleo.net", "taleo"), ("icims.com", "icims"), ("jobvite.com", "jobvite"), ("workable.com", "workable"),
    ("breezy.hr", "breezy"), ("join.com", "join"), ("infojobs.net", "infojobs"),
)


def default_db_path():
    return Path(os.getenv("JOBHUNTER_DB_PATH") or REPO / "data" / "jobs.db")


def default_profile_path():
    return Path(os.getenv("JOBHUNTER_PROFILE_PATH") or REPO / "data" / "master-profile.json")


def default_output_dir():
    return Path(os.getenv("JOBHUNTER_OUTPUT_DIR") or REPO / "data" / "output")


# ── Preflight ────────────────────────────────────────────────────────────────

def probe_browser(url=BROWSER_URL, *, get=None):
    """True when the dedicated Chromium answers on its DevTools port."""
    get = get or requests.get
    try:
        response = get(url, timeout=3)
        return response.status_code == 200 and isinstance(response.json(), dict)
    except Exception:  # noqa: BLE001 - any failure means unavailable
        return False


def load_profile(path):
    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return profile if isinstance(profile, dict) else None


def missing_application_defaults(profile):
    defaults = (profile or {}).get("application_defaults") or {}
    return [key for key in REQUIRED_DEFAULTS
            if not isinstance(defaults, dict) or defaults.get(key) in (None, "")]


def jobs_mailbox():
    try:
        from jobhunter_integrations import gmail_auth
        return gmail_auth.expected_account()
    except Exception:  # noqa: BLE001 - unset mailbox is reported, not fatal
        return None


def platform_hint(url):
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return "unknown"
    for needle, name in PLATFORM_HOSTS:
        if needle in host:
            return name
    return host or "unknown"


# ── Research, package, answers ───────────────────────────────────────────────

def run_research(job, *, conn, now):
    """(research dict | None, note | None) via jobhunter_research, imported at call time."""
    try:
        from jobhunter_research import research
    except ImportError:
        return None, "research module unavailable"
    try:
        result = research(job, conn=conn, now=now)
        if conn is not None:
            conn.commit()
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        return payload, None
    except Exception as exc:  # noqa: BLE001 - research gaps are reported, never fatal
        return None, f"research failed ({type(exc).__name__})"


def _research_file(job_id):
    return policy_module.state_dir() / "autopilot-research" / f"{job_id}.json"


def remember_research(job_id, payload):
    """Keep this run's research for the per-offer report: estimate-only results skip the shared cache."""
    if not payload:
        return
    path = _research_file(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.chmod(name, 0o600)
        os.replace(name, path)
    except OSError:
        pass


def cached_research(conn, job_id):
    """The research the plan used: the shared cache first, then this autopilot's own copy."""
    if store.table_exists(conn, "job_research"):
        row = conn.execute("SELECT payload_json FROM job_research WHERE job_id=?", (job_id,)).fetchone()
        try:
            payload = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            return payload
    try:
        payload = json.loads(_research_file(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def evidence_proposal(manifest):
    """``{"added", "removed", "rounds"}`` when the package used a per-job evidence proposal, else None."""
    if not isinstance(manifest, dict) or manifest.get("evidence_proposal_applied") is not True:
        return None
    added = [str(item.get("evidence_id")) for item in manifest.get("proposed_evidence_updates") or []
             if isinstance(item, dict) and item.get("evidence_id")]
    removed = [str(item) for item in manifest.get("proposed_evidence_omissions") or [] if item]
    rounds = manifest.get("content_review_revision_rounds")
    return {"added": added, "removed": removed, "rounds": rounds if isinstance(rounds, int) else 0}


def read_manifest(directory):
    try:
        manifest = json.loads((Path(directory) / "tailoring_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def package_evidence_proposal(conn, job_id):
    """The evidence proposal recorded in the job's latest package (``applications.package_path``)."""
    if not store.table_exists(conn, "applications"):
        return None
    row = conn.execute("SELECT package_path FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                       (job_id,)).fetchone()
    return evidence_proposal(read_manifest(row[0])) if row and row[0] else None


def _package_dict(package):
    return {"dir": str(package.package_dir), "resume_pdf": str(package.resume_pdf),
            "cover_pdf": str(package.cover_pdf) if package.cover_pdf else None,
            "evidence_proposal": evidence_proposal(read_manifest(package.package_dir))}


def existing_package(conn, job_id, profile):
    """The latest package for this job when it was built from the current profile."""
    if not store.table_exists(conn, "applications"):
        return None
    row = conn.execute("SELECT package_path FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                       (job_id,)).fetchone()
    if not row or not row[0]:
        return None
    directory = Path(row[0])
    resume = directory / "Resume.pdf"
    manifest = read_manifest(directory)
    if manifest is None or not resume.is_file() or manifest.get("job_id") != job_id:
        return None
    if profile is None or manifest.get("profile_sha256") != interest_flow._profile_digest(profile):
        return None
    cover = directory / "CoverLetter.pdf"
    return {"dir": str(directory), "resume_pdf": str(resume), "cover_pdf": str(cover) if cover.is_file() else None,
            "evidence_proposal": evidence_proposal(manifest)}


def make_package(job_id, *, db_path, profile_path, output_dir, **options):
    """Quiet package generation: records package_generated, sends nothing.

    Unattended packages may use a per-job evidence proposal (confirmed public
    wording only; the confirmed variant is never changed). The manual Apply
    flow does not pass this flag.
    """
    options.setdefault("accept_evidence_proposals", True)
    package = interest_flow.prepare_application_package(
        job_id, db_path=db_path, profile_path=profile_path, output_dir=output_dir, **options)
    return _package_dict(package)


def run_answers(profile, research, *, mailbox, cached=None):
    """(answers | None, note | None) via jobhunter_auto_apply.answers, imported at call time."""
    try:
        from jobhunter_auto_apply.answers import build_answers
    except ImportError:
        return None, "answers module unavailable"
    try:
        return build_answers(profile or {}, research=research, jobs_mailbox=mailbox, cached=cached), None
    except Exception as exc:  # noqa: BLE001 - the agent hard-stops on missing answers
        return None, f"answers failed ({type(exc).__name__})"


# ── Hand-offs ────────────────────────────────────────────────────────────────

def latest_application(conn, job_id):
    if not store.table_exists(conn, "applications"):
        return None
    row = conn.execute("SELECT stage, submitted_at FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                       (job_id,)).fetchone()
    return (str(row[0] or ""), row[1]) if row else None


def after_submit(conn, job_id):
    return store.already_submitted(conn, job_id)


def research_card(job):
    """The manual flow's research card for this job (same builders as callback_handler)."""
    for build in (interest_flow.research_job, interest_flow.build_default_research):
        try:
            return interest_flow.build_research_brief_message(job, build(job))
        except Exception:  # noqa: BLE001 - fall back to stored facts, then to a minimal card
            continue
    return (f"🔎 <b>Research</b>\n\n<b>{messages.esc(job.get('title'), 120)}</b>\n"
            f"{messages.esc(job.get('company'), 80)} — {messages.esc(job.get('location'), 80)}\n\n"
            "Choose next step:")


def block_and_hand_off(conn, job, code, detail="", *, run_id, policy, sender, now=None, sync=True):
    """Record the hard stop, then send the research card once per job within the run's hand-off cap.

    Returns ``{"job_id", "reason", "status", "telegram"}`` where telegram is
    sent | already_sent | failed | skipped | cap.
    """
    now = now or datetime.now(timezone.utc)
    job_id = job["id"]
    reason = f"{code}: {detail}" if detail else code
    submitted_already = after_submit(conn, job_id)
    if not submitted_already:
        scraper.record_application_stage(conn, job_id, "blocked_auto_apply", error=reason, now=now, sync=sync)
    current = store.get_log(conn, run_id, job_id)
    outcome = {"job_id": job_id, "title": job.get("title"), "company": job.get("company"), "reason": reason}
    if current and current.get("telegram_notified_at") and current["status"] == "handed_off":
        store.upsert_log(conn, run_id, job_id, "handed_off", reason=reason, now=now)
        return dict(outcome, status="handed_off", telegram="already_sent")
    store.upsert_log(conn, run_id, job_id, "blocked", reason=reason, now=now)
    if store.count_status(conn, run_id, "handed_off") >= policy["max_handoffs_per_run"]:
        return dict(outcome, status="blocked", telegram="cap")
    if sender is None:
        return dict(outcome, status="blocked", telegram="skipped")
    text = messages.with_card(messages.handoff(job, code, detail, after_submit=submitted_already),
                              research_card(job))
    if not sender.send(text, interest_flow.research_brief_keyboard(job_id, job.get("url"))):
        return dict(outcome, status="blocked", telegram="failed")
    scraper.record_job_feedback(conn, job_id, RESEARCH_DELIVERED_ACTION, source=RESEARCH_CARD_SOURCE, now=now)
    store.upsert_log(conn, run_id, job_id, "handed_off", reason=reason, now=now, commit=False)
    store.mark_notified(conn, run_id, job_id, now=now)
    return dict(outcome, status="handed_off", telegram="sent")


# ── The plan ─────────────────────────────────────────────────────────────────

def _skeleton(policy, *, run_id, now, dry_run, active):
    return {
        "schema": SCHEMA, "run_id": run_id, "generated_at": store.iso(now),
        "enabled": bool(policy["enabled"]), "paused": policy_module.is_paused(policy, now),
        "mode": policy["mode"], "dry_run": bool(dry_run), "active": active, "policy": policy,
        "cap": {"max_per_day": policy["max_per_day"], "used_today": 0, "remaining": 0},
        "browser": {"cdp_port": CDP_PORT, "ok": False},
        "preflight": {"missing_application_defaults": [], "jobs_mailbox": None},
        "applications": [], "deferred": [], "closed": [], "handoffs": [], "counts": {}, "notes": [],
    }


def _entry(job, *, research, package, answers, availability_result, criteria):
    apply_url = scraper.official_apply_url(job) or None
    return {
        "job_id": job["id"], "title": job.get("title"), "company": job.get("company"),
        "location": job.get("location"), "listing_url": job.get("url"), "apply_url": apply_url,
        "platform_hint": platform_hint(apply_url or job.get("url")),
        "score": job.get("effective_score", job.get("score")), "score_basis": job.get("score_basis", "stored"),
        "verdict": job.get("verdict") or {}, "criteria": criteria,
        "availability": availability_result, "package": package, "research": research, "answers": answers,
    }


def _copy_database(db_path, directory):
    target = Path(directory) / "jobs.db"
    with closing(sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(str(target))) as copy:
        source.backup(copy)
    return target


def build_plan(db_path, policy, *, now=None, dry_run=False, no_package=False, no_research=False,
               output_dir=None, profile_path=None, sender=None, check=None, monitor=None,
               probe=None, lock_path=None):
    """Return ``(plan, exit_code)``; 0 ok, 3 when another run holds the lock."""
    now = now or datetime.now(timezone.utc)
    run_id = store.run_id_for(now, policy["timezone"])
    active = policy_module.is_active(policy, now)
    plan = _skeleton(policy, run_id=run_id, now=now, dry_run=dry_run, active=active)
    profile_path = Path(profile_path or default_profile_path())
    profile = load_profile(profile_path)
    mailbox = jobs_mailbox()
    plan["preflight"] = {"missing_application_defaults": missing_application_defaults(profile),
                         "profile_found": profile is not None, "jobs_mailbox": mailbox}
    plan["browser"]["ok"] = bool((probe or probe_browser)())
    if not dry_run and not active:
        plan["notes"].append("autopilot disabled" if not policy["enabled"] else "autopilot paused")
        return plan, 0
    if not dry_run and not plan["browser"]["ok"]:
        # Packaging now would only mark jobs interested that nobody can apply to.
        plan["blocked_reason"] = codes.BROWSER_UNAVAILABLE
        plan["notes"].append("dedicated browser is not answering on its DevTools port; nothing planned")
        return plan, 0
    if not dry_run and not no_package and profile is None:
        plan["blocked_reason"] = codes.PACKAGE_NOT_READY
        plan["notes"].append("candidate profile not found; nothing planned")
        return plan, 0
    if dry_run:
        return _build(plan, db_path, policy, now=now, dry_run=True, no_package=no_package,
                      no_research=no_research, output_dir=output_dir, profile_path=profile_path,
                      profile=profile, mailbox=mailbox, sender=None, check=check, monitor=monitor), 0
    with store.autopilot_lock(lock_path) as acquired:
        if not acquired:
            plan["locked"] = True
            plan["notes"].append("another autopilot plan is running")
            return plan, 3
        return _build(plan, db_path, policy, now=now, dry_run=False, no_package=no_package,
                      no_research=no_research, output_dir=output_dir, profile_path=profile_path,
                      profile=profile, mailbox=mailbox, sender=sender, check=check, monitor=monitor), 0


def _build(plan, db_path, policy, *, now, dry_run, no_package, no_research, output_dir, profile_path,
           profile, mailbox, sender, check, monitor):
    run_id = plan["run_id"]
    notes = plan["notes"]
    conn = store.connect(db_path, read_only=dry_run)
    scratch = tempfile.mkdtemp(prefix="autopilot-dry-run-") if dry_run and not no_package else None
    package_db = _copy_database(db_path, scratch) if scratch else Path(db_path)
    package_dir = Path(scratch) / "output" if scratch else Path(output_dir or default_output_dir())
    try:
        if not dry_run:
            store.ensure_schema(conn)
        today = store.log_rows(conn, run_id)
        carried = [row for row in today if row["status"] in store.ACTIVE_STATUSES]
        exclude = {row["job_id"] for row in today if row["status"] not in store.RETRY_STATUSES}
        result = selection.select(conn, policy, now=now, exclude_ids=exclude)
        rehearsal = policy["mode"] == "rehearsal"
        used = store.cap_used_today(conn, run_id, include_rehearsed=rehearsal)
        remaining = max(0, policy["max_per_day"] - used)
        plan["cap"] = {"max_per_day": policy["max_per_day"], "used_today": used, "remaining": remaining}

        def log(job_id, status, **kwargs):
            if not dry_run:
                store.upsert_log(conn, run_id, job_id, status, now=now, **kwargs)

        # Today's packaged/applying rows first: a retry rebuilds, never re-picks.
        for row in carried:
            try:
                job = interest_flow.fetch_job(conn, row["job_id"])
            except KeyError:
                continue
            research = None if no_research else (cached_research(conn, job["id"])
                                                 or run_research(job, conn=None if dry_run else conn, now=now)[0])
            package = existing_package(conn, job["id"], profile)
            if package is None and not no_package:
                # The profile changed or the package dir was cleaned since the
                # row was packaged: rebuild, or hand the job off rather than
                # leave the gate open for a job without a resume.
                try:
                    package = make_package(job["id"], db_path=package_db, profile_path=profile_path,
                                           output_dir=package_dir)
                except Exception as exc:  # noqa: BLE001 - any failure means no CV to upload
                    detail = " ".join(str(exc).split())[:200] or type(exc).__name__
                    label = {"job_id": job["id"], "title": job.get("title"), "company": job.get("company")}
                    if dry_run:
                        plan["handoffs"].append(dict(label, reason=f"{codes.PACKAGE_NOT_READY}: {detail}",
                                                     status="blocked", telegram="skipped"))
                    else:
                        plan["handoffs"].append(block_and_hand_off(
                            conn, job, codes.PACKAGE_NOT_READY, detail, run_id=run_id, policy=policy,
                            sender=sender, now=now))
                    continue
            answers, note = run_answers(profile, research, mailbox=mailbox)
            if note:
                notes.append(f"{job['id']}: {note}")
            score, basis = selection.effective_score(job, now=now)
            job.update(effective_score=score, score_basis=basis, verdict=selection._verdict(job))
            plan["applications"].append(_entry(job, research=research, package=package, answers=answers,
                                               availability_result={"state": "open", "reason": "checked earlier today"},
                                               criteria=row["criteria"]))
        slots = max(0, remaining - sum(1 for row in carried if row["status"] == "packaged"))
        carried_ids = {row["job_id"] for row in carried}
        candidates = [job for job in result["selected"] if job["id"] not in carried_ids]
        closed_tracked = {}
        index, package_failures, stop = 0, 0, False
        while slots > 0 and index < len(candidates) and not stop:
            chunk = candidates[index:index + max(availability.WORKERS, slots)]
            index += len(chunk)
            checks = availability.check_jobs(chunk, check=check)
            for job in chunk:
                if slots <= 0:
                    break
                checked = checks[job["id"]]
                state = availability.state_of(checked) if dry_run else availability.record(
                    conn, job, checked, owned_interested=job.get("status") == "interested")
                seen = {"state": state, "reason": checked.get("reason"), "checked_at": checked.get("checked_at")}
                label = {"job_id": job["id"], "title": job.get("title"), "company": job.get("company")}
                if state == "closed":
                    is_tracked = availability.tracked(conn, job["id"])
                    if is_tracked:
                        closed_tracked[job["id"]] = checked
                    plan["closed"].append(dict(label, tracked=is_tracked, sheet_synced=False))
                    log(job["id"], "closed", criteria=job["criteria"], reason=checked.get("reason"))
                    continue
                if state != "open":
                    plan["deferred"].append(dict(label, reason=checked.get("reason") or "unknown"))
                    log(job["id"], "deferred", criteria=job["criteria"],
                        reason=f"availability unknown: {checked.get('reason')}")
                    continue
                log(job["id"], "selected", criteria=job["criteria"])
                research, note = (None, None) if no_research else run_research(
                    job, conn=None if dry_run else conn, now=now)
                if note:
                    notes.append(f"{job['id']}: {note}")
                if not dry_run:
                    remember_research(job["id"], research)
                package = None
                if not no_package:
                    try:
                        package = existing_package(conn, job["id"], profile) or make_package(
                            job["id"], db_path=package_db, profile_path=profile_path, output_dir=package_dir)
                    except interest_flow.TailoringReadinessError as exc:
                        # Job-specific until it repeats: a second failure in one run
                        # looks systemic (e.g. the summary model is down), so stop
                        # instead of blocking every candidate for good.
                        package_failures += 1
                        detail = " ".join(str(exc).split())[:200]
                        if dry_run:
                            plan["handoffs"].append(dict(label, reason=f"{codes.PACKAGE_NOT_READY}: {detail}",
                                                         status="blocked", telegram="skipped"))
                        else:
                            plan["handoffs"].append(block_and_hand_off(
                                conn, job, codes.PACKAGE_NOT_READY, detail, run_id=run_id, policy=policy,
                                sender=sender, now=now))
                        if package_failures >= MAX_PACKAGE_FAILURES:
                            notes.append("package generation failed twice; planning stopped for this run")
                            stop = True
                            break
                        continue
                    except Exception as exc:  # noqa: BLE001 - rendering/IO failures are not the job's fault
                        plan["deferred"].append(dict(label, reason=f"package error ({type(exc).__name__})"))
                        log(job["id"], "deferred", reason=f"package error ({type(exc).__name__})")
                        notes.append(f"package generation failed ({type(exc).__name__}); planning stopped")
                        stop = True
                        break
                    if not dry_run:
                        conn.execute("UPDATE jobs SET status='interested' WHERE id=? AND status<>'interested'",
                                     (job["id"],))
                        conn.commit()
                answers, note = run_answers(profile, research, mailbox=mailbox)
                if note:
                    notes.append(f"{job['id']}: {note}")
                log(job["id"], "packaged" if package else "selected")
                plan["applications"].append(_entry(job, research=research, package=package, answers=answers,
                                                   availability_result=seen, criteria=job["criteria"]))
                slots -= 1
        if closed_tracked and not dry_run:
            if sender is None:
                notes.append("tracked closures left for the 17:00 listing check (Telegram not configured)")
            else:
                synced, note = availability.sync_tracked_closures(db_path, closed_tracked, send=sender.send,
                                                                  monitor=monitor)
                if note:
                    notes.append(note)
                for item in plan["closed"]:
                    item["sheet_synced"] = synced.get(item["job_id"], False)
        if not dry_run:
            store.update_run(conn, run_id, plan_finished_at=store.iso(now))
        plan["counts"] = {
            "selected": len(result["selected"]), "needs_review": len(result["needs_review"]),
            "filtered": result["filtered"], "carried_over": len(carried),
            "planned": len(plan["applications"]), "deferred": len(plan["deferred"]),
            "closed": len(plan["closed"]), "handoffs": len(plan["handoffs"]),
        }
        if scratch:
            plan["dry_run_output"] = str(package_dir)
        return plan
    finally:
        conn.close()
        if scratch:
            Path(package_db).unlink(missing_ok=True)
            if not (Path(package_dir).exists() and any(Path(package_dir).iterdir())):
                shutil.rmtree(scratch, ignore_errors=True)
