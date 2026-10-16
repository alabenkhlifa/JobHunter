"""Command line for the autopilot; the Hermes wrappers call ``main(argv)``.

Exit codes: 0 ok · 1 internal error · 2 invalid input/blocked · 3 not
verified/pending · 4 refused by policy · 5 Telegram unconfirmed (state saved).
Every command prints one JSON object (``config`` prints the policy summary
unless ``--json``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jobhunter_interest_flow as interest_flow
import scraper

from . import codes, messages, plan, review, store
from . import policy as policy_module

OK, ERROR, INVALID, PENDING, REFUSED, TELEGRAM = 0, 1, 2, 3, 4, 5
RECEIPT_SCHEMA = "jobhunter.receipt/1"
RECEIPT_MAX_AGE = timedelta(minutes=30)
RECEIPT_CLOCK_SKEW = timedelta(minutes=2)
MAX_DRAFT_BYTES = 20_000
DEFAULT_KEYS = {
    "notice_period": "text", "citizenship": "text", "current_work_location": "text",
    "requires_visa_sponsorship": "yes_no", "allow_contact_for_other_vacancies": "yes_no",
}
YES_NO = {"yes": True, "y": True, "true": True, "no": False, "n": False, "false": False}


class CliError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def emit(payload):
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")


# ── Context ──────────────────────────────────────────────────────────────────

def _now(args):
    return getattr(args, "now", None) or datetime.now(timezone.utc)


def _policy(args):
    try:
        return policy_module.load(args.policy)
    except policy_module.PolicyError as exc:
        raise CliError(INVALID, str(exc)) from None


def _db(args):
    return Path(args.db).expanduser() if args.db else plan.default_db_path()


def _connect(args, *, read_only=False):
    path = _db(args)
    if not path.exists():
        raise CliError(INVALID, f"database not found: {path}")
    conn = store.connect(path, read_only=read_only)
    if not read_only:
        store.ensure_schema(conn)
    return conn


def _sender(args):
    """(sender | None, wanted): wanted is False with --no-telegram."""
    if args.no_telegram:
        return None, False
    return messages.telegram_from_env(), True


def _job(conn, job_id):
    try:
        return interest_flow.fetch_job(conn, job_id)
    except KeyError:
        raise CliError(INVALID, f"job not found: {job_id}") from None


def _todays_row(conn, run_id, job_id, allowed=None):
    row = store.get_log(conn, run_id, job_id)
    if row is None:
        raise CliError(INVALID, f"job {job_id} is not in today's autopilot run {run_id}")
    if allowed is not None and row["status"] not in allowed:
        raise CliError(INVALID, f"job {job_id} is {row['status']} in today's run", status=row["status"])
    return row


# ── config ───────────────────────────────────────────────────────────────────

def _show(args, current):
    if getattr(args, "json", False):
        emit(current)
    else:
        sys.stdout.write(policy_module.summary_text(current, _now(args)) + "\n")
    return OK


def _save(args, updated):
    try:
        saved = policy_module.save(updated, args.policy, via=args.via, now=_now(args))
    except policy_module.PolicyError as exc:
        raise CliError(INVALID, str(exc)) from None
    return _show(args, saved)


def cmd_config(args):
    current = _policy(args)
    action = args.config_action
    if action == "show":
        return _show(args, current)
    if action == "set":
        updated = current
        try:
            for assignment in args.assignments:
                updated = policy_module.apply_update(updated, assignment)
        except policy_module.PolicyError as exc:
            raise CliError(INVALID, str(exc)) from None
        return _save(args, updated)
    if action == "defaults":
        return cmd_defaults(args)
    updated = dict(current)
    if action == "enable":
        updated["enabled"] = True
    elif action == "disable":
        updated["enabled"] = False
    elif action == "pause":
        updated["paused"] = True
        try:
            updated["paused_until"] = (policy_module.parse_until(args.until, now=_now(args), tz=current["timezone"])
                                       if args.until else None)
        except policy_module.PolicyError as exc:
            raise CliError(INVALID, str(exc)) from None
    elif action == "resume":
        updated["paused"], updated["paused_until"] = False, None
    elif action in ("live", "rehearsal"):
        updated["mode"] = action
    elif action == "reset":
        updated = policy_module.defaults()
    return _save(args, updated)


def _profile_path(args):
    return Path(args.profile).expanduser() if args.profile else plan.default_profile_path()


def cmd_defaults(args):
    if args.defaults_action != "set":
        raise CliError(INVALID, "use: config defaults set key=value …")
    updates = {}
    for assignment in args.assignments:
        key, sep, value = assignment.partition("=")
        key, value = key.strip(), " ".join(value.split())
        if not sep or key not in DEFAULT_KEYS:
            raise CliError(INVALID, f"unknown application default {key!r}; settable: {', '.join(DEFAULT_KEYS)}")
        if DEFAULT_KEYS[key] == "yes_no":
            if value.lower() not in YES_NO:
                raise CliError(INVALID, f"{key} must be yes or no")
            updates[key] = YES_NO[value.lower()]
        else:
            if not value or len(value) > 160:
                raise CliError(INVALID, f"{key} must be 1-160 characters")
            updates[key] = value
    if not updates:
        raise CliError(INVALID, "nothing to set")
    import resume_refiner
    path = _profile_path(args)
    try:
        backup = resume_refiner.atomic_update_profile(path, {"application_defaults": updates},
                                                      candidate_confirmed=False)
    except FileNotFoundError:
        raise CliError(INVALID, "candidate profile not found") from None
    except resume_refiner.ProfileValidationError as exc:
        raise CliError(INVALID, str(exc)) from None
    profile = plan.load_profile(path)
    emit({"ok": True, "updated": sorted(updates), "backup": str(backup),
          "missing_application_defaults": plan.missing_application_defaults(profile)})
    return OK


# ── Review (cron A) ──────────────────────────────────────────────────────────

def cmd_review_batch(args):
    current = _policy(args)
    conn = _connect(args, read_only=True)
    try:
        emit(review.review_batch(conn, current, _now(args), limit=args.limit))
    finally:
        conn.close()
    return OK


def cmd_record_review(args):
    raw = sys.stdin.read()
    try:
        verdicts = json.loads(raw or "[]")
    except ValueError as exc:
        raise CliError(INVALID, f"invalid JSON on stdin: {exc}") from None
    if not isinstance(verdicts, list) or not all(isinstance(entry, dict) for entry in verdicts):
        raise CliError(INVALID, "expected a JSON array of verdict objects on stdin")
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    conn = _connect(args)
    try:
        pool, _ = review.review_pool(conn, current, now=now, run_id=run_id)
        result = review.record_review(conn, verdicts, pool, now=now)
        store.update_run(conn, run_id, review_finished_at=store.iso(now))
    finally:
        conn.close()
    emit(result)
    return OK


# ── Plan, package, answers (cron B pre-run and agent helpers) ────────────────

def cmd_plan(args):
    current = _policy(args)
    path = _db(args)
    if not path.exists():
        raise CliError(INVALID, f"database not found: {path}")
    sender = None if args.dry_run else _sender(args)[0]
    result, code = plan.build_plan(
        path, current, now=_now(args), dry_run=args.dry_run, no_package=args.no_package,
        no_research=args.no_research, output_dir=args.output_dir, profile_path=_profile_path(args),
        sender=sender)
    emit(result)
    return code


def _reuse_summary(conn, job_id):
    """The last package's reviewed summary, plus its evidence additions so the selection matches it."""
    row = conn.execute("SELECT package_path FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                       (job_id,)).fetchone() if store.table_exists(conn, "applications") else None
    try:
        manifest = json.loads((Path(row[0]) / "tailoring_manifest.json").read_text(encoding="utf-8"))
        review_record = dict(manifest["summary_review"])
    except (TypeError, OSError, ValueError, KeyError):
        raise CliError(INVALID, "no reviewed summary to reuse; package the job first") from None
    review_record.pop("review_source", None)
    proposal = plan.evidence_proposal(manifest)
    return review_record, (proposal or {}).get("added", [])


def cmd_package(args):
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    conn = _connect(args)
    try:
        _job(conn, args.job_id)
        row = store.get_log(conn, run_id, args.job_id)
        if row is None or row["status"] not in store.ACTIVE_STATUSES:
            raise CliError(REFUSED, f"job {args.job_id} is not packaged for today's run")
        options = {}
        if args.cover_draft:
            if current["cover_letter"] == "never":
                raise CliError(REFUSED, "the policy never sends cover letters")
            draft_path = Path(args.cover_draft)
            try:
                if draft_path.stat().st_size > MAX_DRAFT_BYTES:
                    raise CliError(INVALID, "cover draft is larger than 20 KB")
                options["cover_letter_draft"] = json.loads(draft_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise CliError(INVALID, "cover draft is not readable JSON") from None
            options["include_cover_letter"] = True
        if args.reuse_summary:
            options["resume_summary_draft"], options["prior_evidence_ids"] = _reuse_summary(conn, args.job_id)
    finally:
        conn.close()
    try:
        package = plan.make_package(args.job_id, db_path=_db(args), profile_path=_profile_path(args),
                                    output_dir=args.output_dir or plan.default_output_dir(), **options)
    except interest_flow.TailoringReadinessError as exc:
        raise CliError(INVALID, " ".join(str(exc).split())[:300],
                       hard_stop=codes.COVER_LETTER_FAILED if args.cover_draft else codes.PACKAGE_NOT_READY) from None
    except (ValueError, TypeError) as exc:
        raise CliError(INVALID, " ".join(str(exc).split())[:300]) from None
    emit({"ok": True, "job_id": args.job_id, "package": package})
    return OK


def cmd_answers(args):
    conn = _connect(args, read_only=True)
    try:
        job = _job(conn, args.job_id)
        research = plan.cached_research(conn, job["id"])
    finally:
        conn.close()
    answers, note = plan.run_answers(plan.load_profile(_profile_path(args)), research,
                                     mailbox=plan.jobs_mailbox())
    if answers is None:
        raise CliError(ERROR, note or "answers unavailable")
    emit({"ok": True, "job_id": args.job_id, "answers": answers})
    return OK


# ── Outcomes (agent) ─────────────────────────────────────────────────────────

def validate_receipt(path, job_id, now):
    """(receipt, None) when it proves a verified, fresh submission of this job; else (None, why)."""
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "receipt is not readable JSON"
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        return None, f"receipt schema must be {RECEIPT_SCHEMA}"
    if receipt.get("job_id") != job_id:
        return None, "receipt belongs to another job"
    if receipt.get("verified") is not True:
        return None, "receipt is not verified"
    try:
        checked = datetime.fromisoformat(str(receipt.get("checked_at")).replace("Z", "+00:00"))
    except ValueError:
        return None, "receipt checked_at is not an ISO time"
    if checked.tzinfo is None:
        return None, "receipt checked_at has no timezone"
    if now - checked > RECEIPT_MAX_AGE:
        return None, "receipt is older than 30 minutes"
    if checked - now > RECEIPT_CLOCK_SKEW:
        return None, "receipt is dated in the future"
    return receipt, None


def _evidence_file(value):
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_file() else None


def _notify_offer(conn, args, *, run_id, job, row, mode, tracker_synced, evidence, now):
    """Send the per-offer message once (and its screenshot); returns the exit code."""
    if row.get("telegram_notified_at"):
        return OK, "already_sent"
    sender, wanted = _sender(args)
    if not wanted:
        return OK, "skipped"
    if sender is None:
        return TELEGRAM, "not_configured"
    text = messages.per_offer(job, mode=mode, criteria=row.get("criteria"),
                              research=plan.cached_research(conn, job["id"]),
                              tracker_synced=tracker_synced, evidence_attached=evidence is not None,
                              evidence_proposal=plan.package_evidence_proposal(conn, job["id"]))
    if not sender.send(text):
        return TELEGRAM, "failed"
    store.mark_notified(conn, run_id, job["id"], now=now)
    if evidence is not None and not sender.document(evidence, caption=f"Evidence · {messages.esc(job.get('title'), 120)}"):
        return TELEGRAM, "document_failed"
    return OK, "sent"


def cmd_record_submitted(args):
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    conn = _connect(args)
    try:
        job = _job(conn, args.job_id)
        row = _todays_row(conn, run_id, args.job_id)
        receipt, problem = validate_receipt(args.receipt, args.job_id, now)
        if problem:
            if row["status"] != "submitted":
                store.upsert_log(conn, run_id, args.job_id, "unverified", reason=problem, now=now)
            emit({"ok": False, "job_id": args.job_id, "status": "unverified", "error": problem})
            return PENDING
        if row["status"] == "submitted":
            tracker_synced = row.get("reason") == "tracker_synced"
        else:
            stage = scraper.record_application_stage(
                conn, args.job_id, "submitted", evidence_path=receipt.get("evidence_path"),
                application_url=receipt.get("final_url") or receipt.get("page_url"),
                notes=f"autopilot {run_id}", now=now, sync=True, return_receipt=True)
            tracker_synced = stage.tracker_sync_succeeded is True
            conn.execute("UPDATE jobs SET status='submitted' WHERE id=?", (args.job_id,))
            store.upsert_log(conn, run_id, args.job_id, "submitted",
                             reason="tracker_synced" if tracker_synced else "tracker_pending", now=now)
        row = store.get_log(conn, run_id, args.job_id)
        code, telegram = _notify_offer(conn, args, run_id=run_id, job=job, row=row, mode="live",
                                       tracker_synced=tracker_synced,
                                       evidence=_evidence_file(receipt.get("evidence_path")), now=now)
    finally:
        conn.close()
    emit({"ok": code == OK, "job_id": args.job_id, "status": "submitted", "tracker_synced": tracker_synced,
          "telegram": telegram})
    return code


def cmd_rehearsed(args):
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    conn = _connect(args)
    try:
        job = _job(conn, args.job_id)
        row = _todays_row(conn, run_id, args.job_id, allowed=("packaged", "applying", "rehearsed"))
        if row["status"] != "rehearsed":
            store.upsert_log(conn, run_id, args.job_id, "rehearsed", reason=None, now=now)
        row = store.get_log(conn, run_id, args.job_id)
        code, telegram = _notify_offer(conn, args, run_id=run_id, job=job, row=row, mode="rehearsal",
                                       tracker_synced=None, evidence=_evidence_file(args.evidence), now=now)
    finally:
        conn.close()
    emit({"ok": code == OK, "job_id": args.job_id, "status": "rehearsed", "telegram": telegram})
    return code


def cmd_blocked(args):
    if not codes.is_valid(args.reason):
        raise CliError(INVALID, f"unknown hard-stop code {args.reason!r}")
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    detail = " ".join(str(args.detail or "").split())[:300]
    conn = _connect(args)
    try:
        job = _job(conn, args.job_id)
        row = _todays_row(conn, run_id, args.job_id)
        if row["status"] in ("submitted", "rehearsed", "closed"):
            raise CliError(INVALID, f"job {args.job_id} is already {row['status']} today")
        sender, wanted = _sender(args)
        outcome = plan.block_and_hand_off(conn, job, args.reason, detail, run_id=run_id, policy=current,
                                          sender=sender, now=now)
    finally:
        conn.close()
    emit(dict(outcome, ok=True))
    if wanted and outcome["telegram"] in ("failed", "skipped"):
        return TELEGRAM
    return OK


# ── Summary and status ───────────────────────────────────────────────────────

def _labels(conn, rows):
    ids = [row["job_id"] for row in rows]
    if not ids:
        return {}
    found = conn.execute(f"SELECT id, title, company FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {job_id: {"title": title, "company": company} for job_id, title, company in found}


def run_stats(conn, current, run_id):
    rows = store.log_rows(conn, run_id)
    labels = _labels(conn, rows)

    def items(*statuses):
        return [dict(labels.get(row["job_id"], {}), job_id=row["job_id"],
                     reason=(row.get("reason") or "").split(":", 1)[0] if row["status"] in ("blocked", "handed_off")
                     else row.get("reason"))
                for row in rows if row["status"] in statuses]

    attempted = ("packaged", "applying", "submitted", "unverified", "rehearsed", "blocked", "handed_off")
    packaged = ("packaged", "applying", "submitted", "unverified", "rehearsed")
    rehearsal = current["mode"] == "rehearsal"
    used = store.cap_used_today(conn, run_id, include_rehearsed=rehearsal)
    return {
        "run_id": run_id, "mode": current["mode"], "rows": len(rows),
        "applied": sum(1 for row in rows if row["status"] == "submitted"),
        "planned": sum(1 for row in rows if row["status"] in attempted),
        "submitted": items("submitted"), "rehearsed": items("rehearsed"), "unverified": items("unverified"),
        "not_attempted": items("packaged", "applying"), "handed_off": items("handed_off", "blocked"),
        "closed": items("closed"), "deferred": items("deferred"),
        "cap_left": max(0, current["max_per_day"] - used), "max_per_day": current["max_per_day"],
        "evidence_proposals": sum(1 for row in rows if row["status"] in packaged
                                  and plan.package_evidence_proposal(conn, row["job_id"])),
    }


def cmd_summary(args):
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    conn = _connect(args)
    try:
        stats = run_stats(conn, current, run_id)
        run = store.get_run(conn, run_id) or {}
        if run.get("summary_sent_at") and not args.force:
            emit({"ok": True, "sent": False, "already_sent_at": run["summary_sent_at"], "stats": stats})
            return OK
        store.update_run(conn, run_id, summary_json=json.dumps(stats, ensure_ascii=False, default=str))
        if not stats["rows"]:
            emit({"ok": True, "sent": False, "reason": "nothing to report", "stats": stats})
            return OK
        sender, wanted = _sender(args)
        if not wanted:
            emit({"ok": True, "sent": False, "reason": "telegram disabled", "stats": stats,
                  "message": messages.summary(stats)})
            return OK
        if sender is None or not sender.send(messages.summary(stats)):
            emit({"ok": False, "sent": False, "reason": "telegram unconfirmed", "stats": stats})
            return TELEGRAM
        store.update_run(conn, run_id, summary_sent_at=store.iso(now))
    finally:
        conn.close()
    emit({"ok": True, "sent": True, "stats": stats})
    return OK


def cmd_status(args):
    current = _policy(args)
    now = _now(args)
    run_id = store.run_id_for(now, current["timezone"])
    path = _db(args)
    payload = {"policy": current, "summary": policy_module.summary_text(current, now), "run_id": run_id,
               "active": policy_module.is_active(current, now)}
    if path.exists():
        conn = store.connect(path, read_only=True)
        try:
            payload["run"] = store.get_run(conn, run_id)
            payload["stats"] = run_stats(conn, current, run_id)
            payload["log"] = [{k: row[k] for k in ("job_id", "status", "reason", "updated_at", "telegram_notified_at")}
                              for row in store.log_rows(conn, run_id)]
        finally:
            conn.close()
    emit(payload)
    return OK


# ── Parser ───────────────────────────────────────────────────────────────────

GLOBAL_OPTIONS = (
    (("--db",), {"help": "jobs database (default: JOBHUNTER_DB_PATH or data/jobs.db)"}),
    (("--output-dir",), {"help": "package output directory (default: data/output)"}),
    (("--policy",), {"help": "policy file (default: ~/.jobhunter/state/auto_apply.json)"}),
    (("--profile",), {"help": "candidate profile (default: data/master-profile.json)"}),
    (("--no-telegram",), {"action": "store_true", "help": "send nothing; for local runs"}),
    (("--via",), {"choices": policy_module.VIA, "help": "who changed the policy (default cli)"}),
)
GLOBAL_DEFAULTS = {"db": None, "output_dir": None, "policy": None, "profile": None, "no_telegram": False,
                   "via": "cli"}


def build_parser():
    """Global options are accepted before the command or after it (agents write both)."""
    common = argparse.ArgumentParser(add_help=False)
    for flags, options in GLOBAL_OPTIONS:
        common.add_argument(*flags, **options, default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="jobhunter_autopilot", description=__doc__.splitlines()[0],
                                     parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(subparsers, name, **kwargs):
        return subparsers.add_parser(name, parents=[common], **kwargs)

    config = command(commands, "config", help="show or change the autopilot policy")
    config_actions = config.add_subparsers(dest="config_action", required=True)
    for name in ("show", "enable", "disable", "resume", "live", "rehearsal", "reset"):
        command(config_actions, name).add_argument("--json", action="store_true")
    setter = command(config_actions, "set")
    setter.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    setter.add_argument("--json", action="store_true")
    pause = command(config_actions, "pause")
    pause.add_argument("--until", help="12h, 3d, 2w, a date or an ISO date-time")
    pause.add_argument("--json", action="store_true")
    defaults = command(config_actions, "defaults", help="candidate facts for application forms")
    defaults_actions = defaults.add_subparsers(dest="defaults_action", required=True)
    command(defaults_actions, "set").add_argument("assignments", nargs="+", metavar="KEY=VALUE")

    command(commands, "review-batch", help="cron A pre-run: candidates to review").add_argument("--limit", type=int)
    command(commands, "record-review", help="cron A: verdict array on stdin")

    plan_parser = command(commands, "plan", help="cron B pre-run: plan JSON")
    plan_parser.add_argument("--dry-run", action="store_true")
    plan_parser.add_argument("--no-package", action="store_true")
    plan_parser.add_argument("--no-research", action="store_true")

    package = command(commands, "package", help="regenerate a planned job's package")
    package.add_argument("job_id")
    package.add_argument("--cover-draft", metavar="PATH")
    package.add_argument("--reuse-summary", action="store_true")

    command(commands, "answers", help="form answers for a job").add_argument("job_id")

    submitted = command(commands, "record-submitted", help="record a verified submission")
    submitted.add_argument("job_id")
    submitted.add_argument("--receipt", required=True, metavar="PATH")

    rehearsed = command(commands, "rehearsed", help="record a rehearsal (form filled, not submitted)")
    rehearsed.add_argument("job_id")
    rehearsed.add_argument("--evidence", metavar="PATH", help="screenshot of the filled form")

    blocked = command(commands, "blocked", help="record a hard stop and hand the job back")
    blocked.add_argument("job_id")
    blocked.add_argument("--reason", required=True, metavar="CODE")
    blocked.add_argument("--detail", default="")

    command(commands, "summary", help="send the run summary once").add_argument("--force", action="store_true")
    command(commands, "status", help="policy and today's run")
    return parser


HANDLERS = {
    "config": cmd_config, "review-batch": cmd_review_batch, "record-review": cmd_record_review,
    "plan": cmd_plan, "package": cmd_package, "answers": cmd_answers,
    "record-submitted": cmd_record_submitted, "rehearsed": cmd_rehearsed, "blocked": cmd_blocked,
    "summary": cmd_summary, "status": cmd_status,
}


def main(argv=None, *, now=None):
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return OK if exc.code == 0 else INVALID
    for name, value in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    args.now = now
    try:
        return HANDLERS[args.command](args)
    except CliError as exc:
        emit({"ok": False, "error": exc.message, **exc.extra})
        return exc.code
    except Exception as exc:  # noqa: BLE001 - provider errors may hold secrets; print the type only
        emit({"ok": False, "error": f"internal error ({type(exc).__name__})"})
        return ERROR


if __name__ == "__main__":
    raise SystemExit(main())
