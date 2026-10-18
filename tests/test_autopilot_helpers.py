"""Shared builders for the autopilot tests (no tests here)."""

import json
import sqlite3
from datetime import datetime, timezone

import scraper
from jobhunter_autopilot import policy, store

RUBRIC = scraper.CONFIG["review_rubric"]
NOW = datetime(2026, 10, 15, 19, 40, tzinfo=timezone.utc)  # 20:40 Africa/Tunis
RUN_ID = "2026-10-15"

JOBS_DDL = """CREATE TABLE jobs (
    id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT, source TEXT,
    score INTEGER, date_posted TEXT, date_scraped TEXT, notified INTEGER DEFAULT 0,
    description TEXT DEFAULT '', tech_required TEXT DEFAULT '', tech_nice_to_have TEXT DEFAULT '',
    min_experience INTEGER DEFAULT -1, salary TEXT DEFAULT '', work_model TEXT DEFAULT 'on-site',
    score_breakdown TEXT DEFAULT '', status TEXT DEFAULT 'new', recruiter_name TEXT DEFAULT '',
    recruiter_company TEXT DEFAULT '', recruiter_profile_url TEXT DEFAULT '', company_website TEXT DEFAULT '',
    credibility_notes TEXT DEFAULT '', ai_verdict TEXT DEFAULT '', ai_verdict_reason TEXT DEFAULT '',
    ai_sponsorship TEXT DEFAULT '', ai_rank INTEGER, ai_reviewed_at TEXT DEFAULT '', ai_rubric TEXT DEFAULT '',
    sponsorship_signal TEXT DEFAULT '', sponsorship_evidence TEXT DEFAULT '',
    apply_url TEXT DEFAULT '', language_requirement TEXT DEFAULT '')"""

DESCRIPTION = ("We build cloud-native microservices in Java and Spring Boot on AWS with Kafka. "
               "You need 5+ years of experience with Java, Spring Boot, microservices, Kubernetes "
               "and PostgreSQL. Join our product team in Madrid.")
CURRENT_BREAKDOWN = ("stack 0.98x35, role 0.80x30, seniority 1.00x15, employer 1.00x12, "
                     "freshness 1.00x8, band excellent")

_counter = [1000]


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(JOBS_DDL)
    scraper.init_application_tracking(conn)
    scraper.init_feedback_tracking(conn)
    conn.commit()
    return conn


def add_job(conn, job_id=None, **fields):
    _counter[0] += 1
    number = fields.pop("number", None) or _counter[0]
    job_id = job_id or f"li-{number}"
    job = {
        # One employer per fixture job: the autopilot applies once per employer role.
        "id": job_id, "title": "Senior Backend Engineer", "company": f"Acme Software {number}",
        "location": "Madrid, Spain", "url": f"https://www.linkedin.com/jobs/view/{number}",
        "source": "LinkedIn", "score": 88, "date_posted": "2026-09-20",
        "date_scraped": "2026-09-20T20:00:00+00:00", "description": DESCRIPTION,
        "tech_required": "java, spring boot, microservices, kafka, aws, kubernetes",
        "min_experience": 5, "score_breakdown": CURRENT_BREAKDOWN, "status": "new",
        "ai_verdict": "send", "ai_verdict_reason": "strong java fit", "ai_sponsorship": "no_info",
        "ai_rank": 1, "ai_reviewed_at": "2026-10-14T18:00:00+00:00", "ai_rubric": RUBRIC,
    }
    job.update(fields)
    columns = ", ".join(job)
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({', '.join('?' * len(job))})", tuple(job.values()))
    conn.commit()
    return job


def enabled_policy(**changes):
    p = policy.defaults()
    p["enabled"] = True
    for key, value in changes.items():
        p[key] = value
    return policy.validate(p)


def write_policy(path, p):
    policy.save(p, path, via="cli")
    return path


def open_check(job):
    import jobhunter_availability as availability
    listing = availability._listing(job)
    return {"job_id": job["id"], "state": "open", "reason": "application_control", "url": listing.url,
            "source_job_id": listing.source_id, "matched": True, "checked_at": NOW.isoformat()}


def result_for(job, state):
    import jobhunter_availability as availability
    listing = availability._listing(job)
    reason = {"open": "application_control", "closed": "closed_marker", "unknown": "request_failed"}[state]
    return {"job_id": job["id"], "state": state, "reason": reason, "url": listing.url if listing else "",
            "source_job_id": listing.source_id if listing else "", "matched": state != "unknown",
            "checked_at": NOW.isoformat()}


def research_payload(job_id, **salary):
    base = {"low": 52000, "high": 66000, "currency": "EUR", "period": "year", "basis": "market",
            "ask_amount": 62000, "published_text": "", "confidence": "medium"}
    base.update(salary)
    return {"job_id": job_id, "version": 1, "researched_at": NOW.isoformat(), "salary": base,
            "company": {"name": "Acme Software", "website": "https://acme.example", "summary":
                        "Acme builds payment software. It employs 200 people.", "industry": "Fintech",
                        "size": "201-500", "hq": "Madrid", "sources": []},
            "sources": [{"kind": "market", "name": "Levels.fyi", "url": "https://levels.fyi", "snippet": "",
                         "retrieved_at": NOW.isoformat()}], "notes": []}


class FakeResearch:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return json.loads(json.dumps(self.payload))


def telegram_ok():
    from unittest import mock
    response = mock.Mock(status_code=200)
    response.json.return_value = {"ok": True}
    return response


def log_status(conn, job_id, run_id=RUN_ID):
    row = store.get_log(conn, run_id, job_id)
    return row["status"] if row else None
