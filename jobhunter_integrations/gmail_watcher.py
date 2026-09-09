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

from jobhunter_integrations.gmail_auth import GmailAuthError, gmail_service, write_private_json

POSITIVE_KEYWORDS = [
    "application", "applications", "applied", "interview", "interviews", "shortlist", "shortlisted",
    "assessment", "assessments", "offer letter", "offer letters", "job offer", "job offers", "action required", "recruiter", "recruiters",
    "talent acquisition", "hiring", "move forward", "proceed", "next step",
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
ACCOUNT_NOTICE_SENDERS = {
    "no-reply@accounts.google.com",
    "noreply@accounts.google.com",
    "account-security-noreply@accountprotection.microsoft.com",
}
# These are account events, regardless of incidental role/company keywords.
# Do not block a whole employer domain: Google/Microsoft recruiters are valid.
ACCOUNT_NOTICE_PATTERN = re.compile(
    r"\b(?:"
    r"(?:2 step|two step|two factor|2 factor|multi factor) (?:verification|authentication) "
    r"(?:turned on|turned off|enabled|disabled)|"
    r"security alert|new sign in|unusual sign in|suspicious sign in|"
    r"password (?:reset|changed)|reset your password|"
    r"verify your (?:email|account)|confirm your email|"
    r"verification code|security code|one time (?:code|password|passcode)"
    r")\b"
)
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
    (
        "selection process will not continue",
        r"\b(?:will not|won t|cannot|can t|decided not to) "
        r"(?:be )?(?:continue|continuing|proceed|proceeding)(?: with)? "
        r"(?:the|this|our) (?:(?:hiring|recruitment|selection|application) )?process with you\b",
    ),
]
ACKNOWLEDGEMENT_PATTERNS = (
    r"\b(?:we (?:have )?received|we ve received) your application\b",
    r"\byour application (?:has been|was) (?:received|submitted)\b",
    r"\byour application (?:will be reviewed|is (?:currently )?under review)\b",
)
OFFER_PATTERNS = [
    (
        "offer extended",
        r"\b(?:pleased|delighted|happy|would like|d like) to "
        r"(?:extend|make|present) (?:you )?"
        r"(?:a|an) (?:job |employment )?offer\b",
    ),
    (
        "position offered",
        r"\b(?:pleased|delighted|happy) to offer you (?:the |a )?"
        r"(?:position|role|job)\b",
    ),
    ("offer of employment", r"\boffer of employment\b"),
    (
        "offer issued",
        r"\b(?:your|the) (?:job |employment )?offer (?:is|has been) "
        r"(?:ready|approved|issued|attached|enclosed)\b",
    ),
    ("offer letter", r"\b(?:your|the) offer letter\b|\boffer letter (?:for|regarding)\b"),
]
INTERVIEW_PATTERNS = [
    (
        "interview invitation",
        r"\b(?:(?:invite|invited) (?:you )?(?:to|for)|invitation (?:to|for)) "
        r"(?:an? )?(?:initial |technical |phone |video |onsite |on site )?interview\b",
    ),
    (
        "interview scheduling",
        r"\b(?:would like|d like|want|wish) to (?:schedule|arrange|book) "
        r"(?:an? )?(?:initial |technical |phone |video |onsite |on site )?interview\b",
    ),
    (
        "selected for interview",
        r"\b(?:selected|shortlisted) (?:you )?for (?:an? )?"
        r"(?:initial |technical |phone |video |onsite |on site )?interview\b",
    ),
    (
        "accepted for interview",
        r"\b(?:your application has been|you have been) (?:accepted|progressed|advanced) "
        r"(?:to|for) (?:an? |the )?(?:interview|interview stage|interview process)\b",
    ),
    ("interview request", r"\binterview (?:invitation|request)\b"),
]
ASSESSMENT_PATTERNS = [
    (
        "assessment invitation",
        r"\b(?:(?:invite|invited) (?:you )?(?:to (?:complete |take )?|for )|"
        r"invitation (?:to (?:complete |take )?|for ))(?:the |an? |your )?"
        r"(?:online |technical |coding )?(?:assessment|coding challenge|technical test)\b",
    ),
    (
        "assessment requested",
        r"\b(?:please|kindly) (?:complete|take|submit) (?:the |an? |your )?"
        r"(?:online |technical |coding )?(?:assessment|coding challenge|technical test)\b",
    ),
    (
        "assessment required",
        r"\b(?:assessment|coding challenge|technical test) "
        r"(?:invitation|request|required|is required)\b",
    ),
]
ACTION_REQUIRED_PATTERNS = [
    ("action required", r"\baction required\b"),
    (
        "information requested",
        r"\b(?:please|kindly) (?:provide|submit|upload|complete|confirm) "
        r"(?:the )?(?:additional|required|requested|following) "
        r"(?:information|documents|details|fields)\b",
    ),
    ("additional information required", r"\badditional information (?:is )?required\b"),
]
PROGRESSION_PATTERNS = [
    (
        "application progressed",
        r"\b(?:pleased|happy) to inform you (?:that )?(?:your application|you) "
        r"(?:has|have) (?:progressed|advanced|been shortlisted)\b",
    ),
    (
        "proceeding with application",
        r"\b(?:would like|d like|wish|want) to (?:proceed|progress|move forward) "
        r"with (?:your|the) application\b",
    ),
    (
        "application moving forward",
        r"\b(?:we are|we re) (?:proceeding|progressing|moving forward) "
        r"with (?:your|the) application\b",
    ),
    ("shortlisted", r"\byou (?:have been|were) shortlisted\b"),
    (
        "invited to next stage",
        r"\b(?:invite|invited) (?:you )?to (?:the )?next (?:stage|step)"
        r"(?: of (?:the )?(?:hiring|recruitment) process)?\b",
    ),
    (
        "selected for next stage",
        r"\bselected (?:you )?to (?:proceed|progress|move forward) "
        r"(?:to|with) (?:the )?next (?:stage|step)\b",
    ),
]
OUTCOME_PATTERNS = [
    ("rejected", REJECTION_PATTERNS),
    ("offer_received", OFFER_PATTERNS),
    ("interview_invited", INTERVIEW_PATTERNS),
    ("assessment_requested", ASSESSMENT_PATTERNS),
    ("action_required", ACTION_REQUIRED_PATTERNS),
    ("application_progressed", PROGRESSION_PATTERNS),
]
ACTIVE_EMAIL_STAGES = {
    "submitted",
    "application_progressed",
    "action_required",
    "assessment_requested",
    "interview_invited",
    "offer_received",
}
OUTCOME_ALERTS = {
    "rejected": ("❌", "Application rejected"),
    "offer_received": ("📄", "Job offer received"),
    "interview_invited": ("📅", "Interview invitation received"),
    "assessment_requested": ("🧪", "Assessment requested"),
    "action_required": ("⚠️", "Application action required"),
    "application_progressed": ("➡️", "Application progressed"),
}
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
            raise RuntimeError("Gmail watcher state could not be read; restore it before retrying.") from None
    return dict(default)


def save_json(path: Path, data: dict[str, Any]) -> None:
    write_private_json(path, data)


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
                 OR a.stage IN (
                        'submitted',
                        'draft_ready',
                        'package_generated',
                        'package_prepared',
                        'approved',
                        'application_progressed',
                        'action_required',
                        'assessment_requested',
                        'interview_invited',
                        'offer_received'
                    )
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


def extract_visible_text(payload: dict[str, Any]) -> str:
    """Read visible mail copy, excluding CSS, scripts and HTML attributes."""
    mime = str(payload.get("mimeType", ""))
    chunks = []
    if mime.startswith("text/") and payload.get("body", {}).get("data"):
        body = decode_part_body(payload["body"]["data"])
        if mime == "text/html":
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(body, "html.parser")
            for element in soup(["head", "style", "script", "noscript"]):
                element.decompose()
            body = soup.get_text(" ", strip=True)
        chunks.append(body)
    for part in payload.get("parts", []) or []:
        chunks.append(extract_visible_text(part))
    return "\n".join(chunk for chunk in chunks if chunk)


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


def contains_term(text: str, term: str) -> bool:
    """Match complete words/phrases, never CTO in factor or Lever in delivery."""
    return bool(term and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))


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
    for outcome, patterns in OUTCOME_PATTERNS:
        reasons = [label for label, pattern in patterns if re.search(pattern, text)]
        if reasons:
            return outcome, reasons
    return None, []


def match_active_application(
    message_text: str,
    jobs: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str]:
    text = normalize_for_matching(message_text)
    active = [job for job in jobs if normalize(job.get("stage", "")) in ACTIVE_EMAIL_STAGES]

    exact = [
        job
        for job in active
        if len(normalize_for_matching(job.get("title", ""))) >= 8
        and contains_term(text, normalize_for_matching(job.get("title", "")))
    ]
    if len(exact) == 1:
        return exact[0], "exact job title"
    if len(exact) > 1:
        return None, "multiple active applications share the matched title"

    scored: list[tuple[int, dict[str, str], list[str]]] = []
    for job in active:
        company = normalize_for_matching(job.get("company", ""))
        title_terms = {
            token
            for token in normalize_for_matching(job.get("title", "")).split()
            if len(token) >= 4 and token not in GENERIC_TITLE_TERMS
        }
        matched_terms = sorted(term for term in title_terms if contains_term(text, term))
        company_matched = bool(company and len(company) >= 3 and contains_term(text, company))
        if not matched_terms:
            continue
        score = len(matched_terms) * 20 + (40 if company_matched else 0)
        if company_matched or len(matched_terms) >= 2:
            scored.append((score, job, matched_terms))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None, "multiple active applications matched with equal confidence"
        score, job, matched_terms = scored[0]
        return job, f"company/title terms: {', '.join(matched_terms)} (score {score})"

    company_matches = [
        job
        for job in active
        if len(normalize_for_matching(job.get("company", ""))) >= 3
        and contains_term(text, normalize_for_matching(job.get("company", "")))
    ]
    company_names = {normalize_for_matching(job.get("company", "")) for job in company_matches}
    if len(company_matches) == 1:
        return company_matches[0], "only active application for matched company"
    if len(company_names) == 1 and len(company_matches) > 1:
        return None, "multiple active applications matched the same company"
    return None, "no unique active application matched the email"


def is_relevant(
    message_text: str, jobs: list[dict[str, str]], *, sender: str = "", subject: str = "",
) -> tuple[bool, list[str]]:
    text = normalize(message_text)
    reasons: list[str] = []
    address = email.utils.parseaddr(sender)[1].casefold()
    if address in ACCOUNT_NOTICE_SENDERS:
        return False, []
    if ACCOUNT_NOTICE_PATTERN.search(normalize_for_matching(subject or message_text)):
        return False, []
    if any(noise in text for noise in GOOGLE_SHARE_NOISE):
        return False, []
    hits = [k for k in POSITIVE_KEYWORDS if contains_term(text, k)]
    if any(contains_term(text, noise) for noise in NEGATIVE_NOISE) and not hits:
        return False, []
    if hits:
        reasons.append("keywords: " + ", ".join(hits[:4]))
    matched = sorted(term for term in job_terms(jobs) if contains_term(text, term))
    if matched:
        reasons.append("matches jobs/companies: " + ", ".join(matched[:5]))
    return bool(hits or matched), reasons


def message_summary(service, msg_id: str) -> dict[str, Any]:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    text = extract_visible_text(payload)
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


def record_application_outcome(
    db_path: Path,
    job: dict[str, str],
    outcome: str,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    import scraper

    if outcome not in OUTCOME_ALERTS:
        raise ValueError(f"Unsupported application email outcome: {outcome}")

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
        current_stage = current["stage"]
        if current_stage == outcome:
            return {
                "status": "already_recorded",
                "reason": f"already recorded as {outcome}",
                "tracker_synced": False,
            }
        if current_stage not in ACTIVE_EMAIL_STAGES:
            return {
                "status": "skipped",
                "reason": f"latest application stage {current_stage!r} is not active",
                "tracker_synced": False,
            }
        if outcome == "application_progressed" and current_stage != "submitted":
            return {
                "status": "skipped",
                "reason": f"kept more specific application stage {current_stage!r}",
                "tracker_synced": False,
            }
        if current_stage == "offer_received" and outcome != "rejected":
            return {
                "status": "skipped",
                "reason": "kept more specific application stage 'offer_received'",
                "tracker_synced": False,
            }

        if outcome == "rejected":
            audit_note = f"Rejection detected by Gmail watcher at {timestamp.isoformat(timespec='seconds')}."
            audit_marker = "Rejection detected by Gmail watcher"
        else:
            audit_note = (
                f"Application email classified as {outcome} by Gmail watcher "
                f"at {timestamp.isoformat(timespec='seconds')}."
            )
            audit_marker = f"Application email classified as {outcome} by Gmail watcher"
        existing_notes = (current["notes"] or "").strip()
        notes = existing_notes
        if audit_marker not in existing_notes:
            notes = " | ".join(part for part in (existing_notes, audit_note) if part)

        scraper.record_application_stage(
            conn,
            job["id"],
            outcome,
            notes=notes,
            now=timestamp,
            commit=False,
            sync=False,
        )
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (outcome, job["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    tracker_synced = scraper.sync_application_tracker_if_enabled()
    return {
        "status": "updated",
        "reason": f"application marked {outcome}",
        "tracker_synced": tracker_synced,
    }


def record_rejected_application(
    db_path: Path,
    job: dict[str, str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    return record_application_outcome(db_path, job, "rejected", now=now)


def process_application_outcome(
    summary: dict[str, Any],
    jobs: list[dict[str, str]],
    db_path: Path,
) -> None:
    outcome, outcome_reasons = classify_application_outcome(summary["text"])
    if outcome:
        summary["outcome"] = outcome
        summary["outcome_reasons"] = outcome_reasons
    else:
        normalized = normalize_for_matching(summary["text"])
        summary["acknowledgement"] = any(
            re.search(pattern, normalized)
            for pattern in ACKNOWLEDGEMENT_PATTERNS
        ) and not re.search(
            r"\b(?:unfortunately|regret|unsuccessful|declined|rejected|decided|chosen|other candidates)\b",
            normalized,
        )

    job, match_reason = match_active_application(summary["text"], jobs)
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
    if outcome:
        summary["application_update"] = record_application_outcome(db_path, job, outcome)
    else:
        summary["application_update"] = {
            "status": "unchanged",
            "reason": "receipt acknowledgement" if summary["acknowledgement"] else "outcome needs review",
            "tracker_synced": False,
        }


def format_email_date(value: str, timezone: dt.tzinfo | None = None) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        local = parsed.astimezone(timezone)
        return f"{local.day} {local:%b} · {local:%H:%M}"
    except (TypeError, ValueError, OverflowError):
        return ""


def preview_text(message: dict[str, Any]) -> str:
    text = html.unescape(message.get("snippet") or "")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "", text)
    text = " ".join(text.split())
    subject = " ".join((message.get("subject") or "").split())
    if subject and text.casefold().startswith(subject.casefold()):
        text = text[len(subject):].lstrip(" :-—")
    if len(text) > 140:
        text = text[:141].rsplit(" ", 1)[0] + "…"
    return html.escape(text)


def format_email_sender(value: str) -> str:
    name, address = email.utils.parseaddr(value)
    return html.escape(name or address.rpartition("@")[2])


def format_alert(matches: list[dict[str, Any]]) -> str:
    lines = ["📬 <b>Application updates</b>"]
    for m in matches[:5]:
        if m.get("processing_error"):
            lines.extend(
                [
                    "",
                    "⚠️ <b>Application email processing failed</b>",
                    "The message was left unprocessed so the next watcher run can retry it.",
                ]
            )
            continue

        outcome = m.get("outcome")
        job = m.get("matched_job") or {}
        update = m.get("application_update") or {}
        if outcome in OUTCOME_ALERTS:
            icon, label = OUTCOME_ALERTS[outcome]
        elif m.get("acknowledgement"):
            icon, label = "📨", "Application received"
        else:
            icon, label = "⚠️", "Review needed"
        lines.extend(["", f"{icon} <b>{label}</b>"])
        if job:
            lines.append(f"<b>{html.escape(job.get('company') or 'Unknown company')}</b>")
            lines.append(html.escape(job.get("title") or "Unknown role"))
        else:
            sender = format_email_sender(m.get("from") or "")
            if sender:
                lines.append(sender)
            subject = m.get("subject") or "Application reply"
            if subject.casefold() != label.casefold():
                lines.append(html.escape(subject))

        date = format_email_date(m.get("date") or "")
        if date:
            lines.append(f"🕒 {date}")
        if outcome in OUTCOME_ALERTS:
            if not job:
                lines.append("⚠️ Couldn’t link this email to one application.")
            elif update.get("status") not in {"updated", "already_recorded"}:
                lines.append("⚠️ Application status was not updated.")
            elif update.get("tracker_synced"):
                lines.append("✅ Database and spreadsheet updated")
            else:
                lines.extend(["✅ Outcome recorded in database", "⚠️ Spreadsheet update not confirmed"])
        elif m.get("acknowledgement"):
            lines.append("Receipt confirmation; no status change.")
        else:
            preview = preview_text(m)
            if preview:
                lines.append(preview)
            lines.append("⚠️ Status unchanged — review this email.")
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


def collect_mail(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Inspect mail and calculate the next ledger without committing delivery."""
    service = gmail_service(args.google_token)
    jobs = interested_jobs(args.db_path)
    state = load_json(args.state_path, {"seen_message_ids": [], "last_checked_at": None})
    seen = set(state.get("seen_message_ids") or [])
    new_seen = set(seen)
    matches: list[dict[str, Any]] = []
    page_token = None
    inspected = 0
    while inspected < args.max_messages:
        request = {"userId": "me", "q": args.query, "maxResults": min(args.max_messages, 100)}
        if page_token:
            request["pageToken"] = page_token
        resp = service.users().messages().list(**request).execute()
        for item in resp.get("messages", []) or []:
            msg_id = item["id"]
            if msg_id in new_seen:
                continue
            summary = message_summary(service, msg_id)
            inspected += 1
            relevant, reasons = is_relevant(
                summary["text"], jobs, sender=summary.get("from", ""), subject=summary.get("subject", ""),
            )
            # Verification secrets belong only to the active ATS interaction,
            # never the periodic recruiter digest.
            verification = re.search(
                r"\b(?:verification code|security code|one[- ]time (?:code|password)|otp|verify your (?:email|account)|confirm your email)\b",
                summary["text"], re.IGNORECASE,
            )
            if relevant and not verification:
                summary["reasons"] = reasons
                try:
                    process_application_outcome(summary, jobs, args.db_path)
                except Exception as exc:
                    summary["processing_error"] = type(exc).__name__
                    matches.append(summary)
                    if inspected >= args.max_messages:
                        break
                    continue
                matches.append(summary)
            new_seen.add(msg_id)
            if inspected >= args.max_messages:
                break
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    # Do not evict IDs while they can still match the query: eviction replays
    # old alerts. Keep the small ID ledger, independently of mailbox read flags.
    state["seen_message_ids"] = sorted(new_seen)
    state["last_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return matches, state


def check_mail(args: argparse.Namespace) -> list[dict[str, Any]]:
    matches, state = collect_mail(args)
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
    from dotenv import load_dotenv
    load_dotenv(default_repo_root() / ".env")
    try:
        matches = check_mail(parse_args(argv))
    except GmailAuthError as exc:
        print(f"⚠️ JobHunter Gmail unavailable: {exc}")
        return 1
    if matches:
        print(format_alert(matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
