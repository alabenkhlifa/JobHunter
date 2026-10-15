import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import jobhunter_research
from jobhunter_research import cache, salary

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
JOB = {
    "id": "sdj-9",
    "title": "Senior Software Engineer",
    "company": "Acme",
    "location": "Valencia, Spain",
    "source": "Spain Dev Jobs",
    "salary": "€60k–70k / year",
    "description": "",
    "company_website": "https://acme.es/",
}


@pytest.fixture(autouse=True)
def no_benchmarks(tmp_path, monkeypatch):
    monkeypatch.setattr(salary, "BENCHMARKS_PATH", tmp_path / "missing.json")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    yield connection
    connection.close()


def offline(query, **kwargs):
    return []


def test_schema_matches_the_contract_ddl(conn):
    cache.ensure_schema(conn)
    cache.ensure_schema(conn)  # idempotent

    columns = [row[1] for row in conn.execute("PRAGMA table_info(job_research)")]
    assert columns == ["job_id", "researched_at", "version", "salary_low", "salary_high", "currency", "period",
                       "basis", "ask_amount", "confidence", "company_summary", "company_website", "sources_json",
                       "payload_json"]


def test_research_stores_and_reuses_a_fresh_result(conn):
    first = jobhunter_research.research(JOB, conn=conn, search=offline, fetch=lambda url: None, now=NOW)
    row = conn.execute(
        "SELECT salary_low, salary_high, currency, period, basis, ask_amount, confidence, company_website, payload_json "
        "FROM job_research WHERE job_id = 'sdj-9'"
    ).fetchone()

    assert row[:8] == (60000, 70000, "EUR", "year", "published", 68000, "high", "https://acme.es")
    assert json.loads(row[8]) == first.to_dict()

    def must_not_search(query, **kwargs):
        raise AssertionError("cached research must not search")

    again = jobhunter_research.research(JOB, conn=conn, search=must_not_search, now=NOW + timedelta(days=13))
    assert again.to_dict() == first.to_dict()


def test_stale_or_other_version_rows_are_ignored(conn):
    jobhunter_research.research(JOB, conn=conn, search=offline, fetch=lambda url: None, now=NOW)

    assert cache.load(conn, "sdj-9", 14, now=NOW + timedelta(days=15)) is None
    assert cache.load(conn, "sdj-9", 14, now=NOW, version=99) is None
    assert cache.load(conn, "missing", 14, now=NOW) is None


def test_estimate_only_results_are_cached_for_one_day_then_retried(conn):
    job = dict(JOB, salary="", source="LinkedIn", id="li-3")

    result = jobhunter_research.research(job, conn=conn, search=offline, fetch=lambda url: None, now=NOW)

    assert result.salary["basis"] == "none"
    # The engine reads the ask from this row, so even a weak result is stored...
    assert conn.execute("SELECT basis FROM job_research WHERE job_id='li-3'").fetchone()[0] == "none"
    assert cache.load(conn, "li-3", 14, now=NOW + timedelta(hours=20)) is not None
    # ...but it expires after a day so blocked providers are retried.
    assert cache.load(conn, "li-3", 14, now=NOW + timedelta(days=1, minutes=1)) is None


def test_store_updates_in_place(conn):
    cache.ensure_schema(conn)
    result = jobhunter_research.research(JOB, search=offline, fetch=lambda url: None, now=NOW)
    cache.store(conn, result)
    result.salary["ask_amount"] = 69000
    cache.store(conn, result)

    assert conn.execute("SELECT COUNT(*), MAX(ask_amount) FROM job_research").fetchone() == (1, 69000)


def test_round_trip_preserves_the_contract_shape():
    result = jobhunter_research.research(JOB, search=offline, fetch=lambda url: None, now=NOW)

    assert jobhunter_research.ResearchResult.from_dict(json.loads(json.dumps(result.to_dict()))).to_dict() == result.to_dict()
