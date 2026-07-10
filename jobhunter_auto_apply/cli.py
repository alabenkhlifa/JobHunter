#!/usr/bin/env python3
"""CLI for JobHunter's safe auto-apply engine."""

from __future__ import annotations

import argparse
import json
import sys

from .engine import ApplyConfig, AutoApplyEngine, inspection_to_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Approval-gated JobHunter auto-apply helper")
    parser.add_argument("--db", default="data/jobs.db", help="Path to JobHunter SQLite DB")
    parser.add_argument("--output-dir", default="data/output", help="Directory for evidence screenshots")
    parser.add_argument("--cdp-host", default="127.0.0.1")
    parser.add_argument("--cdp-port", type=int, default=9222)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspect current browser page and record draft/blocker state")
    inspect.add_argument("--job-id", required=True)
    inspect.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")

    upload = sub.add_parser("upload", help="Upload a file to a file input after explicit approval")
    upload.add_argument("--job-id", required=True)
    upload.add_argument("--selector", required=True, help="CSS selector for input[type=file]")
    upload.add_argument("--file", required=True)
    upload.add_argument("--approved", action="store_true", help="Required to perform upload")

    submit = sub.add_parser("submit", help="Click final submit after explicit approval")
    submit.add_argument("--job-id", required=True)
    submit.add_argument("--selector", required=True, help="CSS selector for submit button")
    submit.add_argument("--approved", action="store_true", help="Required to submit")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ApplyConfig(db_path=args.db, output_dir=args.output_dir, cdp_host=args.cdp_host, cdp_port=args.cdp_port)
    engine = AutoApplyEngine(config)

    try:
        if args.command == "inspect":
            inspection = engine.inspect(args.job_id)
        elif args.command == "upload":
            inspection = engine.upload_file(args.job_id, args.selector, args.file, approved=args.approved)
        elif args.command == "submit":
            inspection = engine.click_submit(args.job_id, args.selector, approved=args.approved)
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except PermissionError as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(json.dumps(inspection.__dict__, indent=2, ensure_ascii=False))
    else:
        print(inspection_to_markdown(inspection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
