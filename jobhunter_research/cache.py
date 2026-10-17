"""SQLite cache for research results (table ``job_research``)."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from jobhunter_research import ResearchResult

DDL = """CREATE TABLE IF NOT EXISTS job_research (job_id TEXT PRIMARY KEY, researched_at TEXT NOT NULL, version INTEGER NOT NULL,
  salary_low INTEGER, salary_high INTEGER, currency TEXT, period TEXT, basis TEXT NOT NULL, ask_amount INTEGER,
  confidence TEXT NOT NULL, company_summary TEXT, company_website TEXT, sources_json TEXT NOT NULL DEFAULT '[]', payload_json TEXT NOT NULL)"""


WEAK_BASES = {"estimate", "none"}
WEAK_TTL_DAYS = 1 / 24
PARTIAL_TTL = timedelta(minutes=15)
CONTEXT_FIELDS = ("title", "company", "location", "description", "salary", "source", "company_website", "min_experience")


def context_digest(job: dict, settings: dict | None = None) -> str:
    context = {key: job.get(key) for key in CONTEXT_FIELDS}
    context["settings"] = settings or {}
    return hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(job_research)")}
    if "context_sha256" not in columns:
        conn.execute("ALTER TABLE job_research ADD COLUMN context_sha256 TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load(conn: sqlite3.Connection, job_id: str, max_age_days: float, *, now: datetime | None = None,
         version: int | None = None, context_sha256: str | None = None) -> "ResearchResult | None":
    """The cached result when it is at most ``max_age_days`` old and of the current version."""
    from jobhunter_research import RESEARCH_VERSION, ResearchResult

    row = conn.execute(
        "SELECT researched_at, version, payload_json, context_sha256 FROM job_research WHERE job_id = ?", (str(job_id),)
    ).fetchone()
    if row is None:
        return None
    researched_at, row_version, payload = row[0], row[1], row[2]
    if context_sha256 is not None and row[3] != context_sha256:
        return None
    try:
        current_version = int(row_version)
    except (TypeError, ValueError):
        return None
    if current_version != (RESEARCH_VERSION if version is None else version):
        return None
    when = _parse_time(researched_at)
    now = now or datetime.now(timezone.utc)
    if when is None or when > now or now - when > timedelta(days=max_age_days):
        return None
    try:
        result = ResearchResult.from_dict(json.loads(payload))
    except (TypeError, ValueError, KeyError):
        return None
    # Retain the row for the salary-answer reader, but retry incomplete lookups
    # soon instead of treating a rate limit as evidence that no pay data exists.
    failures = ("failed", "http ", "unavailable", "no parsable", "no search results", "no results", "challenge")
    partial = any(any(term in note.lower() for term in failures) for note in result.notes)
    if partial and now - when > PARTIAL_TTL:
        return None
    if result.salary.get("basis") in WEAK_BASES and now - when > timedelta(days=min(max_age_days, WEAK_TTL_DAYS)):
        return None
    return result


def store(conn: sqlite3.Connection, result: "ResearchResult", *, context_sha256: str = "") -> None:
    payload = result.to_dict()
    salary, company = payload["salary"], payload["company"]
    conn.execute(
        """INSERT INTO job_research (job_id, researched_at, version, salary_low, salary_high, currency, period,
               basis, ask_amount, confidence, company_summary, company_website, sources_json, payload_json, context_sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET researched_at = excluded.researched_at, version = excluded.version,
               salary_low = excluded.salary_low, salary_high = excluded.salary_high, currency = excluded.currency,
               period = excluded.period, basis = excluded.basis, ask_amount = excluded.ask_amount,
               confidence = excluded.confidence, company_summary = excluded.company_summary,
               company_website = excluded.company_website, sources_json = excluded.sources_json,
               payload_json = excluded.payload_json, context_sha256 = excluded.context_sha256""",
        (
            payload["job_id"], payload["researched_at"], payload["version"], salary["low"], salary["high"],
            salary["currency"], salary["period"], salary["basis"], salary["ask_amount"], salary["confidence"],
            company["summary"] or None, company["website"] or None,
            json.dumps(payload["sources"], ensure_ascii=False), json.dumps(payload, ensure_ascii=False), context_sha256,
        ),
    )
    conn.commit()
