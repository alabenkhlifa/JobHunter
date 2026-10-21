#!/usr/bin/env python3
"""CLI for JobHunter's safe auto-apply engine.

Exit codes: 0 ok · 1 internal error · 2 invalid input/blocked · 3 not verified/pending ·
4 refused by the autopilot policy gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .cdp import CDPError
from .email_verification import EmailVerificationError
from .engine import ApplyConfig, AutoApplyEngine, GateRefused, inspection_to_markdown


EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_UNVERIFIED, EXIT_REFUSED = 0, 1, 2, 3, 4

EPILOG = """\
Browser: the CDP port defaults to 9222; the Pi's dedicated Chromium listens on 9333,
so pass --cdp-port 9333 there. --page-url must be the tab's exact current URL and
must be unique: if two tabs share that URL the connection fails, so keep one tab per job.

--policy-run (autopilot) replaces --approved with the autopilot policy gate; a refusal
(disabled, paused, rehearsal submit, cap reached, already submitted) exits 4.
Exit codes: 0 ok, 1 internal error, 2 invalid input/blocked, 3 not verified/pending,
4 refused by the policy gate.
"""

PAGE_URL_HELP = "Exact URL of the application tab (must be unique across open tabs)"


def _approval(parser: argparse.ArgumentParser, approved_help: str | None) -> None:
    group = parser.add_mutually_exclusive_group()
    if approved_help is not None:
        group.add_argument("--approved", action="store_true", help=approved_help)
    group.add_argument("--policy-run", action="store_true",
                       help="Unattended autopilot run: authorized by the policy gate instead of --approved")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Approval-gated JobHunter auto-apply helper", epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/jobs.db", help="Path to JobHunter SQLite DB")
    parser.add_argument("--output-dir", default="data/output", help="Directory for evidence screenshots")
    parser.add_argument("--cdp-host", default="127.0.0.1")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Chromium CDP port (Pi browser: 9333)")
    parser.add_argument("--policy", default=None, help="Autopilot policy file for --policy-run (gate default otherwise)")
    parser.add_argument("--candidate-profile", default="data/master-profile.json",
                        help="Confirmed profile for reviewing ATS CV parser output")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspect current browser page and record draft/blocker state")
    inspect.add_argument("--job-id", required=True)
    inspect.add_argument("--page-url", help=PAGE_URL_HELP)
    inspect.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    inspect.add_argument("--answers", help="Policy runs: JSON answers (build_answers output or a plan entry)")
    inspect.add_argument("--profile", default="data/master-profile.json",
                         help="Policy runs without --answers: owner profile used to build answers")
    _approval(inspect, None)

    reconcile = sub.add_parser('reconcile-prefill', help='Audit ATS CV extraction against the approved package')
    reconcile.add_argument('--job-id', required=True)
    reconcile.add_argument('--page-url', required=True, help=PAGE_URL_HELP)
    reconcile.add_argument('--approved', action='store_true', help='Repair unambiguous source-backed fields; otherwise audit only')

    advance = sub.add_parser('advance', help='Advance one reviewed step after checking CV parser output')
    advance.add_argument('--job-id', required=True)
    advance.add_argument('--page-url', required=True, help=PAGE_URL_HELP)
    advance.add_argument('--selector', required=True)
    advance.add_argument('--approved', action='store_true', help='Owner approved preparation and the completed answers')

    upload = sub.add_parser("upload", help="Upload a file to a file input after explicit approval")
    upload.add_argument("--job-id", required=True)
    upload.add_argument("--page-url", required=True, help=PAGE_URL_HELP)
    upload.add_argument("--selector", required=True, help="CSS selector for input[type=file]")
    upload.add_argument("--file", required=True)
    _approval(upload, "Required to perform upload")

    submit = sub.add_parser("submit", help="Click final submit after explicit approval")
    submit.add_argument("--job-id", required=True)
    submit.add_argument("--page-url", required=True, help=PAGE_URL_HELP)
    submit.add_argument("--selector", required=True, help="CSS selector for submit button")
    submit.add_argument("--json", action="store_true", help="Emit JSON (policy runs include the receipt)")
    _approval(submit, "Required to submit")

    verify = sub.add_parser("verify-email", help="Resume a pending email-code step without resubmitting")
    verify.add_argument("--job-id", required=True)
    verify.add_argument("--page-url", required=True, help=PAGE_URL_HELP)
    _approval(verify, "Required to enter the emailed code")

    credential = sub.add_parser("credential", help="Fill ATS account email/password from the vault (never printed)")
    credential.add_argument("--job-id", required=True)
    credential.add_argument("--page-url", required=True, help=PAGE_URL_HELP)
    credential.add_argument("--ats-key", required=True, help="ATS host, e.g. acme.wd3.myworkdayjobs.com")
    credential.add_argument("--email-selector", required=True)
    credential.add_argument("--password-selector", required=True)
    credential.add_argument("--confirm-selector", help="Password confirmation input (account creation)")
    credential.add_argument("--create", action="store_true",
                            help="No stored account: generate a strong password and store it in the vault")
    credential.add_argument("--mailbox", help="Override the jobs mailbox (default: configured Gmail account)")
    _approval(credential, "Required to type stored credentials")

    receipt = sub.add_parser("verify-receipt", help="Verify the confirmation page; exit 0 verified, 3 unverified")
    receipt.add_argument("--job-id", required=True)
    receipt.add_argument("--page-url", required=True,
                         help="URL of the submitted application tab (the tab may have navigated on the same host)")
    receipt.add_argument("--out", help="Receipt path (default: <output-dir>/<job>/evidence/receipt.json)")

    return parser


def _answers(args) -> dict | None:
    if not getattr(args, "policy_run", False) or args.command != "inspect":
        return None
    from .answers import load_answers

    return load_answers(args.db, args.job_id, profile_path=args.profile, answers_path=args.answers)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy_run = bool(getattr(args, "policy_run", False))
    try:
        answers = _answers(args)
    except (OSError, ValueError) as exc:
        print(f"Blocked: answers unavailable: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    config = ApplyConfig(
        db_path=args.db,
        output_dir=args.output_dir,
        cdp_host=args.cdp_host,
        cdp_port=args.cdp_port,
        expected_page_url=args.page_url,
        profile_path=args.candidate_profile,
        verify_submission=True,
        policy_run=policy_run,
        policy_path=args.policy,
        answers=answers,
    )
    engine = AutoApplyEngine(config)
    approved = bool(getattr(args, "approved", False))

    try:
        if args.command == 'reconcile-prefill':
            result = engine.reconcile_prefill(args.job_id, approved=approved)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return EXIT_BLOCKED if result['issues'] else EXIT_OK
        elif args.command == 'advance':
            inspection = engine.advance(args.job_id, args.selector, approved=approved)
        elif args.command == "inspect":
            inspection = engine.inspect(args.job_id)
        elif args.command == "upload":
            inspection = engine.upload_file(args.job_id, args.selector, args.file, approved=approved)
        elif args.command == "submit":
            inspection = engine.click_submit(args.job_id, args.selector, approved=approved)
        elif args.command == "verify-email":
            inspection = engine.verify_email(args.job_id, approved=approved)
        elif args.command == "credential":
            result = engine.fill_credentials(
                args.job_id, ats_key=args.ats_key, email_selector=args.email_selector,
                password_selector=args.password_selector, confirm_selector=args.confirm_selector,
                create=args.create, approved=approved, mailbox=args.mailbox)
            print(json.dumps(result, ensure_ascii=False))
            return EXIT_OK
        elif args.command == "verify-receipt":
            receipt = engine.verify_receipt(args.job_id, out_path=args.out)
            print(json.dumps(receipt, indent=2, ensure_ascii=False))
            if not receipt["verified"]:
                print("Unverified: no confirmation marker naming this job was found.", file=sys.stderr)
                return EXIT_UNVERIFIED
            return EXIT_OK
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except GateRefused as exc:
        print(f"Refused: {' '.join(str(exc).split())}", file=sys.stderr)
        return EXIT_REFUSED
    except (PermissionError, CDPError, OSError) as exc:
        if policy_run and isinstance(exc, EmailVerificationError):
            print(f"Pending: {exc}", file=sys.stderr)
            return EXIT_UNVERIFIED
        print(f"Blocked: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except Exception:
        if args.command == "credential":
            # Never let an unexpected error message carry the secret.
            print("Error: credential filling failed.", file=sys.stderr)
            return EXIT_ERROR
        raise

    if getattr(args, "json", False):
        payload = asdict(inspection)
        if policy_run and engine.last_plan is not None:
            payload["question_plan"] = engine.last_plan
            payload["hard_stops"] = engine.last_plan["hard_stops"]
            payload["run_id"] = engine.config.run_id
            payload["mode"] = engine.run_mode
        if policy_run and args.command == "submit":
            payload["receipt"] = engine.last_receipt
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(inspection_to_markdown(inspection))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
