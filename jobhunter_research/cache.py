"""SQLite cache for research results (table ``job_research``)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from jobhunter_research import ResearchResult

DDL = """CREATE TABLE IF NOT EXISTS job_research (job_id TEXT PRIMARY KEY, researched_at TEXT NOT NULL, version INTEGER NOT NULL,
  salary_low INTEGER, salary_high INTEGER, currency TEXT, period TEXT, basis TEXT NOT NULL, ask_amount INTEGER,
  confidence TEXT NOT NULL, company_summary TEXT, company_website TEXT, sources_json TEXT NOT NULL DEFAULT '[]', payload_json TEXT NOT NULL)"""


WEAK_BASES = {"estimate", "none"}
WEAK_TTL_DAYS = 1


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
    conn.commit()


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load(conn: sqlite3.Connection, job_id: str, max_age_days: float, *, now: datetime | None = None,
         version: int | None = None) -> "ResearchResult | None":
    """The cached result when it is at most ``max_age_days`` old and of the current version."""
    from jobhunter_research import RESEARCH_VERSION, ResearchResult

    row = conn.execute(
        "SELECT researched_at, version, payload_json FROM job_research WHERE job_id = ?", (str(job_id),)
    ).fetchone()
    if row is None:
        return None
    researched_at, row_version, payload = row[0], row[1], row[2]
    if int(row_version) != (RESEARCH_VERSION if version is None else version):
        return None
    when = _parse_time(researched_at)
    now = now or datetime.now(timezone.utc)
    if when is None or now - when > timedelta(days=max_age_days):
        return None
    try:
        result = ResearchResult.from_dict(json.loads(payload))
    except (TypeError, ValueError, KeyError):
        return None
    # Benchmark-only or empty results usually mean the providers were blocked:
    # keep them one day for the engine's salary answer, then research again.
    if result.salary.get("basis") in WEAK_BASES and now - when > timedelta(days=min(max_age_days, WEAK_TTL_DAYS)):
        return None
    return result


def store(conn: sqlite3.Connection, result: "ResearchResult") -> None:
    payload = result.to_dict()
    salary, company = payload["salary"], payload["company"]
    conn.execute(
        """INSERT INTO job_research (job_id, researched_at, version, salary_low, salary_high, currency, period,
               basis, ask_amount, confidence, company_summary, company_website, sources_json, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET researched_at = excluded.researched_at, version = excluded.version,
               salary_low = excluded.salary_low, salary_high = excluded.salary_high, currency = excluded.currency,
               period = excluded.period, basis = excluded.basis, ask_amount = excluded.ask_amount,
               confidence = excluded.confidence, company_summary = excluded.company_summary,
               company_website = excluded.company_website, sources_json = excluded.sources_json,
               payload_json = excluded.payload_json""",
        (
            payload["job_id"], payload["researched_at"], payload["version"], salary["low"], salary["high"],
            salary["currency"], salary["period"], salary["basis"], salary["ask_amount"], salary["confidence"],
            company["summary"] or None, company["website"] or None,
            json.dumps(payload["sources"], ensure_ascii=False), json.dumps(payload, ensure_ascii=False),
        ),
    )
    conn.commit()
