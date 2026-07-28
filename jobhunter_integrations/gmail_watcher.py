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
GOOGLE_SHARE_NOISE = [
    "drive-shares-dm-noreply@google.com",
    "via google sheets",
    "spreadsheet shared with you",
    "shared a spreadsheet",
    "has invited you to edit the following spreadsheet",
    "google sheets",
    "google drive",
]
REJECTION_PATTERNS = [
    ("regret to inform", r"\bregret to inform (?:you|the candidate)\b"),
    (
        "application not progressing",
        r"\b(?:(?:will|would|can|could) not|won t|cannot) "
        r"(?:be )?(?:progressing|proceeding|moving forward)"
        r"(?: with)? (?:your|the) application\b",
    ),
    (
        "application unsuccessful",
        r"\b(?:your|the) application (?:(?:has|have) not been successful|"
        r"(?:has been|was|is) (?:not successful|unsuccessful))\b",
    ),
    (
        "application not taken forward",
        r"\b(?:will|would|have|has) not (?:be )?(?:take|taking|taken) "
        r"(?:your|the) application (?:any )?further\b",
    ),
    (
        "decision not to proceed",
        r"\b(?:decided|chosen) not to (?:proceed|progress|move forward)(?: with)? "
        r"(?:your|the) application\b",
    ),
    (
        "not selected",
        r"\b(?:you have|you've|you were|you are|your application was|your application has) "
        r"(?:not|not been) selected\b",
    ),
    (
        "other candidates selected",
        r"\b(?:move forward with|progress with|pursue|selected) (?:another|other) candidates?\b",
    ),
    (
        "application no longer considered",
        r"\b(?:your|the) application is no longer (?:under consideration|being considered)\b",
    ),
    (
        "unable to progress application",
        r"\bunable to (?:progress|proceed|move forward)(?: with)? (?:your|the) application\b",
    ),
]
GENERIC_TITLE_TERMS = {
    "architect",
    "backend",
    "consultant",
    "developer",
    "engineer",
    "lead",
    "manager",
    "senior",
    "software",
    "solution",
    "solutions",
    "specialist",
    "technical",
    "technology",
}


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
            LEFT JOIN applications a ON a.id = (
                SELECT a2.id
                FROM applications a2
                WHERE a2.job_id = j.id
                ORDER BY a2.id DESC
                LIMIT 1
            )
            WHERE (
                    j.status IN ('interested', 'submitted')
                 OR a.stage IN ('submitted', 'draft_ready', 'package_generated', 'package_prepared', 'approved')
            )
              AND COALESCE(a.stage, '') NOT IN ('rejected', 'withdrawn')
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


def normalize_for_matching(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


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


def classify_application_outcome(message_text: str) -> tuple[str | None, list[str]]:
    text = normalize_for_matching(message_text)
    reasons = [label for label, pattern in REJECTION_PATTERNS if re.search(pattern, text)]
    return ("rejected", reasons) if reasons else (None, [])


def match_submitted_application(
    message_text: str,
    jobs: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str]:
    text = normalize_for_matching(message_text)
    submitted = [job for job in jobs if normalize(job.get("stage", "")) == "submitted"]

    exact = [
        job
        for job in submitted
        if len(normalize_for_matching(job.get("title", ""))) >= 8
        and normalize_for_matching(job.get("title", "")) in text
    ]
    if len(exact) == 1:
        return exact[0], "exact job title"
    if len(exact) > 1:
        return None, "multiple submitted applications share the matched title"

    scored: list[tuple[int, dict[str, str], list[str]]] = []
    for job in submitted:
        company = normalize_for_matching(job.get("company", ""))
        title_terms = {
            token
            for token in normalize_for_matching(job.get("title", "")).split()
            if len(token) >= 4 and token not in GENERIC_TITLE_TERMS
        }
        matched_terms = sorted(term for term in title_terms if term in text)
        company_matched = bool(company and len(company) >= 3 and company in text)
        if not matched_terms:
            continue
        score = len(matched_terms) * 20 + (40 if company_matched else 0)
        if company_matched or len(matched_terms) >= 2:
            scored.append((score, job, matched_terms))

    if not scored:
        return None, "no unique submitted application matched the email"
    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, "multiple submitted applications matched with equal confidence"
    score, job, matched_terms = scored[0]
    return job, f"company/title terms: {', '.join(matched_terms)} (score {score})"


def is_relevant(message_text: str, jobs: list[dict[str, str]]) -> tuple[bool, list[str]]:
    text = normalize(message_text)
    reasons: list[str] = []
    if any(noise in text for noise in GOOGLE_SHARE_NOISE):
        return False, []
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
        "to": header_value(headers, "To"),
        "subject": header_value(headers, "Subject"),
        "date": header_value(headers, "Date"),
        "snippet": msg.get("snippet") or "",
        "text": (
            f"{header_value(headers, 'From')} {header_value(headers, 'To')} "
            f"{header_value(headers, 'Subject')} {msg.get('snippet') or ''} {text}"
        ),
    }


def record_rejected_application(
    db_path: Path,
    job: dict[str, str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    import scraper

    timestamp = now or dt.datetime.now(dt.timezone.utc)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        current = conn.execute(
            """
            SELECT id, stage, notes
            FROM applications
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job["id"],),
        ).fetchone()
        if current is None:
            return {"status": "skipped", "reason": "application record not found", "tracker_synced": False}
        if current["stage"] == "rejected":
            return {"status": "already_rejected", "reason": "already rejected", "tracker_synced": False}
        if current["stage"] != "submitted":
            return {
                "status": "skipped",
                "reason": f"latest application stage is {current['stage']!r}, not 'submitted'",
                "tracker_synced": False,
            }

        audit_note = f"Rejection detected by Gmail watcher at {timestamp.isoformat(timespec='seconds')}."
        existing_notes = (current["notes"] or "").strip()
        notes = existing_notes
        if "Rejection detected by Gmail watcher" not in existing_notes:
            notes = " | ".join(part for part in (existing_notes, audit_note) if part)

        scraper.record_application_stage(
            conn,
            job["id"],
            "rejected",
            notes=notes,
            now=timestamp,
            commit=False,
            sync=False,
        )
        conn.execute("UPDATE jobs SET status = 'rejected' WHERE id = ?", (job["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    tracker_synced = scraper.sync_application_tracker_if_enabled()
    return {"status": "updated", "reason": "application marked rejected", "tracker_synced": tracker_synced}


def process_application_outcome(
    summary: dict[str, Any],
    jobs: list[dict[str, str]],
    db_path: Path,
) -> None:
    outcome, outcome_reasons = classify_application_outcome(summary["text"])
    if not outcome:
        return
    summary["outcome"] = outcome
    summary["outcome_reasons"] = outcome_reasons
    if outcome != "rejected":
        return

    job, match_reason = match_submitted_application(summary["text"], jobs)
    summary["application_match_reason"] = match_reason
    if job is None:
        summary["application_update"] = {
            "status": "skipped",
            "reason": match_reason,
            "tracker_synced": False,
        }
        return

    summary["matched_job"] = {
        "id": job["id"],
        "title": job.get("title", ""),
        "company": job.get("company", ""),
    }
    summary["application_update"] = record_rejected_application(db_path, job)


def format_alert(matches: list[dict[str, Any]]) -> str:
    lines = ["📬 JobHunter application email update"]
    for idx, m in enumerate(matches[:5], 1):
        if m.get("processing_error"):
            lines.extend(
                [
                    "",
                    f"{idx}. ⚠️ Application email processing failed",
                    "The message was left unprocessed so the next watcher run can retry it.",
                ]
            )
            continue

        if m.get("outcome") == "rejected":
            job = m.get("matched_job") or {}
            update = m.get("application_update") or {}
            lines.extend(["", f"{idx}. ❌ Application rejected"])
            if job:
                lines.append(
                    f"Job: {html.escape(job.get('title') or 'Unknown')} — "
                    f"{html.escape(job.get('company') or 'Unknown company')}"
                )
            else:
                lines.append("Job: automatic match was not unique")
            if update.get("status") in {"updated", "already_rejected"}:
                lines.append("Status: rejected")
            else:
                lines.append(f"Status update skipped: {html.escape(update.get('reason') or 'unknown reason')}")
            if update.get("status") == "updated":
                tracker_status = "synced" if update.get("tracker_synced") else "sync did not complete"
                lines.append(f"Application tracker: {tracker_status}")
            lines.append(f"Date: {html.escape(m.get('date') or '')}")
            continue

        subject = html.escape(m.get("subject") or "(no subject)")
        sender = html.escape(email.utils.parseaddr(m.get("from") or "")[1] or m.get("from") or "unknown sender")
        snippet = html.escape(" ".join((m.get("snippet") or "").split())[:240])
        reasons = html.escape("; ".join(m.get("reasons") or []))
        lines.extend(["", f"{idx}. {subject}", f"From: {sender}", f"Date: {html.escape(m.get('date') or '')}", f"Why: {reasons or 'matched JobHunter email heuristics'}", f"Snippet: {snippet}"])
    if len(matches) > 5:
        lines.append(f"\n…and {len(matches) - 5} more matching messages.")
    return "\n".join(lines)


def mark_message_read(service, msg_id: str) -> None:
    """Mark a message read after the watcher has inspected it."""
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except Exception:
        # Do not make notification checks fail solely because label cleanup failed.
        pass


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
        if relevant:
            summary["reasons"] = reasons
            try:
                process_application_outcome(summary, jobs, args.db_path)
            except Exception as exc:
                summary["processing_error"] = type(exc).__name__
                matches.append(summary)
                continue
            matches.append(summary)
        mark_message_read(service, msg_id)
        new_seen.add(msg_id)
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
