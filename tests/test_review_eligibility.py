from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

import scraper


NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def job(**overrides):
    return dict({
        "id": "example", "title": "Backend Architect", "company": "Example",
        "location": "Dubai, United Arab Emirates", "score": 80,
        "notified": 0, "status": "new", "min_experience": -1,
        "description": "5+ years of backend engineering experience.",
        "date_posted": "", "date_scraped": NOW.isoformat(),
    }, **overrides)


@pytest.mark.parametrize("posted,scraped,eligible", [
    ((NOW - timedelta(days=8)).isoformat(), NOW.isoformat(), False),
    ((NOW - timedelta(days=7, hours=23)).isoformat(), NOW.isoformat(), True),
    ("", (NOW - timedelta(days=8)).isoformat(), False),
    ("", NOW.isoformat(), True),
    ("not a date", NOW.isoformat(), True),
    ("", "", False),
    ("", "not a date", False),
    ("2026-09-08", NOW.isoformat(), True),
])
def test_freshness_uses_posting_date_then_collection_date(posted, scraped, eligible):
    _, reason = scraper.prepare_review_candidate(
        job(date_posted=posted, date_scraped=scraped), now=NOW,
    )
    assert (reason is None) is eligible


def test_experience_requirement_after_introduction_overrides_missing_metadata():
    original = job(description="Company introduction. " * 100 +
                   "10+ years architecting and operating distributed infrastructure.")
    prepared, reason = scraper.prepare_review_candidate(original, now=NOW)
    assert prepared["min_experience"] == 10
    assert "over the 8 cap" in reason
    assert original["min_experience"] == -1


def test_company_history_is_not_a_candidate_experience_requirement():
    prepared, reason = scraper.prepare_review_candidate(job(
        description="For 20 years we have served clients. Requirements: 5+ years of backend experience.",
    ), now=NOW)
    assert prepared["min_experience"] == 5
    assert reason is None


@pytest.mark.parametrize("changes", [
    {"location": "Bengaluru, India"},
    {"title": "Junior Backend Engineer"},
    {"description": "Company introduction. " * 100 + "Must hold a valid work permit."},
])
def test_current_hard_filters_are_reapplied(changes):
    _, reason = scraper.prepare_review_candidate(job(**changes), now=NOW)
    assert reason


def make_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE jobs (
        id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, score INTEGER,
        notified INTEGER, status TEXT, min_experience INTEGER, description TEXT,
        date_posted TEXT, date_scraped TEXT, ai_verdict TEXT DEFAULT '',
        ai_verdict_reason TEXT DEFAULT '', ai_sponsorship TEXT DEFAULT '', ai_rank INTEGER
    )""")
    columns = tuple(job())
    for row in rows:
        conn.execute("INSERT INTO jobs (" + ",".join(columns) + ") VALUES (" +
                     ",".join("?" for _ in columns) + ")", [row[key] for key in columns])
    conn.commit()
    return conn


def test_candidate_checks_do_not_change_job_records():
    conn = make_conn([job(), job(id="expired", date_posted="2026-08-01")])
    before = conn.total_changes
    assert [j["id"] for j in scraper.get_review_candidates(conn, now=NOW)] == ["example"]
    assert conn.total_changes == before
    assert conn.execute("SELECT status FROM jobs WHERE id='expired'").fetchone()[0] == "new"
    conn.close()


def test_record_review_rechecks_eligibility_before_writing():
    conn = make_conn([job(date_posted="2000-01-01")])
    result = scraper.record_review(conn, [{
        "job_id": "example", "verdict": "send", "sponsorship": "implied",
        "rank": 1, "reason": "fit",
    }])
    assert result == []
    assert conn.execute("SELECT ai_verdict,notified,status FROM jobs").fetchone()[:] == ("", 0, "new")
    conn.close()


def test_queue_preview_and_digest_count_exclude_expired_backlog():
    recent = datetime.now(timezone.utc).isoformat()
    conn = make_conn([
        job(date_scraped=recent),
        job(id="expired", date_posted="2000-01-01", date_scraped=recent),
    ])
    assert [row["id"] for row in scraper.list_queued_jobs(conn)] == ["example"]
    assert scraper._queued_after_send(conn, []) == [80]
    assert scraper._queued_after_send(conn, ["example"]) == []
    conn.close()
