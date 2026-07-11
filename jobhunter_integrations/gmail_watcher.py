#!/usr/bin/env python3
"""Watch a dedicated jobs Gmail mailbox for recruiter/ATS replies.

All account-specific values are configured through CLI/env/local ignored files.
When used as a cron/no-agent script, it prints nothing if no new relevant mail
is found and prints a concise alert if matching mail arrives.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import html
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
POSITIVE_KEYWORDS = [
    "application", "applied", "interview", "shortlist", "shortlisted",
    "assessment", "recruiter", "talent acquisition", "hiring", "next step",
    "next steps", "thank you for applying", "we received your application",
    "your application", "job application", "workday", "greenhouse", "lever", "avature",
]
NEGATIVE_NOISE = ["newsletter", "unsubscribe", "promotion", "marketing", "security alert"]


def default_repo_root() -> Path:
    return Path(os.getenv("JOBHUNTER_REPO_ROOT", Path.cwd())).resolve()


def default_token_path() -> Path:
    return Path(os.getenv("GOOGLE_TOKEN_PATH", Path.home() / ".jobhunter" / "google_token.json")).expanduser()


def default_state_path() -> Path:
    return Path(os.getenv("JOBHUNTER_GMAIL_WATCHER_STATE", Path.home() / ".jobhunter" / "state" / "gmail_watcher_seen.json")).expanduser()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return dict(default)
    return dict(default)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def gmail_service(token_path: Path):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def interested_jobs(db_path: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT j.id, j.title, j.company, j.url, COALESCE(a.stage, '') AS stage,
                   COALESCE(a.platform, '') AS platform, COALESCE(a.application_url, '') AS application_url
            FROM jobs j
            LEFT JOIN applications a ON a.job_id = j.id
            WHERE j.status IN ('interested', 'submitted')
               OR a.stage IN ('submitted', 'draft_ready', 'package_generated', 'package_prepared', 'approved')
            ORDER BY datetime(j.date_scraped) DESC
            """
        ).fetchall()
        return [dict(r) for r in rows if not str(r["id"]).startswith("test-")]
    finally:
        conn.close()


def decode_part_body(data: str) -> str:
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode()).decode(errors="ignore")
    except Exception:
        return ""


def extract_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    if str(payload.get("mimeType", "")).startswith("text/") and payload.get("body", {}).get("data"):
        chunks.append(decode_part_body(payload["body"]["data"]))
    for part in payload.get("parts", []) or []:
        chunks.append(extract_text(part))
    return "\n".join(c for c in chunks if c)


def header_value(headers: list[dict[str, str]], name: str) -> str:
    name_l = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_l:
            return h.get("value", "")
    return ""


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def job_terms(jobs: list[dict[str, str]]) -> set[str]:
    terms: set[str] = set()
    generic = {"senior", "software", "engineer", "backend", "lead", "tech", "architect", "manager"}
    for job in jobs:
        company = normalize(job.get("company", ""))
        title = normalize(job.get("title", ""))
        if company and len(company) >= 3:
            terms.add(company)
        for token in re.findall(r"[a-z0-9][a-z0-9+.#-]{2,}", title):
            if token not in generic:
                terms.add(token)
    return terms


def is_relevant(message_text: str, jobs: list[dict[str, str]]) -> tuple[bool, list[str]]:
    text = normalize(message_text)
    reasons: list[str] = []
    if any(noise in text for noise in NEGATIVE_NOISE) and not any(k in text for k in POSITIVE_KEYWORDS):
        return False, []
    hits = [k for k in POSITIVE_KEYWORDS if k in text]
    if hits:
        reasons.append("keywords: " + ", ".join(hits[:4]))
    matched = [term for term in job_terms(jobs) if term and term in text]
    if matched:
        reasons.append("matches jobs/companies: " + ", ".join(matched[:5]))
    return bool(hits or matched), reasons


def message_summary(service, msg_id: str) -> dict[str, Any]:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    text = extract_text(payload)
    return {
        "id": msg_id,
        "from": header_value(headers, "From"),
        "subject": header_value(headers, "Subject"),
        "date": header_value(headers, "Date"),
        "snippet": msg.get("snippet") or "",
        "text": f"{header_value(headers, 'From')} {header_value(headers, 'Subject')} {msg.get('snippet') or ''} {text}",
    }


def format_alert(matches: list[dict[str, Any]]) -> str:
    lines = ["📬 JobHunter Gmail watcher: possible recruiter/ATS reply"]
    for idx, m in enumerate(matches[:5], 1):
        subject = html.escape(m.get("subject") or "(no subject)")
        sender = html.escape(email.utils.parseaddr(m.get("from") or "")[1] or m.get("from") or "unknown sender")
        snippet = html.escape(" ".join((m.get("snippet") or "").split())[:240])
        reasons = html.escape("; ".join(m.get("reasons") or []))
        lines.extend(["", f"{idx}. {subject}", f"From: {sender}", f"Date: {html.escape(m.get('date') or '')}", f"Why: {reasons or 'matched JobHunter email heuristics'}", f"Snippet: {snippet}"])
    if len(matches) > 5:
        lines.append(f"\n…and {len(matches) - 5} more matching messages.")
    return "\n".join(lines)


def check_mail(args: argparse.Namespace) -> list[dict[str, Any]]:
    service = gmail_service(args.google_token)
    jobs = interested_jobs(args.db_path)
    state = load_json(args.state_path, {"seen_message_ids": [], "last_checked_at": None})
    seen = set(state.get("seen_message_ids") or [])
    resp = service.users().messages().list(userId="me", q=args.query, maxResults=args.max_messages).execute()
    new_seen = set(seen)
    matches: list[dict[str, Any]] = []
    for item in resp.get("messages", []) or []:
        msg_id = item["id"]
        if msg_id in seen:
            continue
        summary = message_summary(service, msg_id)
        relevant, reasons = is_relevant(summary["text"], jobs)
        new_seen.add(msg_id)
        if relevant:
            summary["reasons"] = reasons
            matches.append(summary)
    state["seen_message_ids"] = sorted(new_seen)[-500:]
    state["last_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save_json(args.state_path, state)
    return matches


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    repo = default_repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo)
    parser.add_argument("--db-path", type=Path, default=Path(os.getenv("JOBHUNTER_DB_PATH", repo / "data" / "jobs.db")))
    parser.add_argument("--google-token", type=Path, default=default_token_path())
    parser.add_argument("--state-path", type=Path, default=default_state_path())
    parser.add_argument("--max-messages", type=int, default=int(os.getenv("JOBHUNTER_GMAIL_WATCHER_MAX", "25")))
    parser.add_argument("--query", default=os.getenv("JOBHUNTER_GMAIL_WATCHER_QUERY", "newer_than:30d -from:me"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    matches = check_mail(parse_args(argv))
    if matches:
        print(format_alert(matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
