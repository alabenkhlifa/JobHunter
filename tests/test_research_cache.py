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
                       "payload_json", "context_sha256"]


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

    again = jobhunter_research.research(JOB, conn=conn, search=must_not_search, now=NOW + timedelta(minutes=5))
    assert again.to_dict() == first.to_dict()


def test_stale_or_other_version_rows_are_ignored(conn):
    jobhunter_research.research(JOB, conn=conn, search=offline, fetch=lambda url: None, now=NOW)

    assert cache.load(conn, "sdj-9", 14, now=NOW + timedelta(days=15)) is None
    assert cache.load(conn, "sdj-9", 14, now=NOW, version=99) is None
    assert cache.load(conn, "missing", 14, now=NOW) is None


def test_estimate_only_results_are_cached_for_one_hour_then_retried(conn):
    job = dict(JOB, salary="", source="LinkedIn", id="li-3")
    result = jobhunter_research.research(job, search=offline, fetch=lambda url: None, now=NOW)
    result.notes = []  # Isolate the weak-result TTL from the separate partial-result TTL.
    from jobhunter_interest_flow import salary_target
    digest = cache.context_digest(job, {"target": salary_target(job), "quantile": 0.75, "round_to": 1000})
    cache.ensure_schema(conn)
    cache.store(conn, result, context_sha256=digest)

    assert result.salary["basis"] == "none"
    # The engine reads the ask from this row, so even a weak result is stored...
    assert conn.execute("SELECT basis FROM job_research WHERE job_id='li-3'").fetchone()[0] == "none"
    assert cache.load(conn, "li-3", 14, now=NOW + timedelta(minutes=59), context_sha256=digest) is not None
    # ...but it expires after an hour so weak results are retried promptly.
    assert cache.load(conn, "li-3", 14, now=NOW + timedelta(hours=1, minutes=1), context_sha256=digest) is None


def test_partial_provider_failures_expire_after_fifteen_minutes(conn):
    result = jobhunter_research.research(JOB, search=offline, fetch=lambda url: None, now=NOW)
    result.notes = ["Levels.fyi fetch failed: Timeout."]
    digest = cache.context_digest(JOB, {"target": None, "quantile": 0.75, "round_to": 1000})
    cache.ensure_schema(conn)
    cache.store(conn, result, context_sha256=digest)

    assert cache.load(conn, "sdj-9", 14, now=NOW + timedelta(minutes=10), context_sha256=digest) is not None
    assert cache.load(conn, "sdj-9", 14, now=NOW + timedelta(minutes=16), context_sha256=digest) is None


def test_old_schema_migration_retains_rows_but_requires_fresh_context(conn):
    conn.execute("""CREATE TABLE job_research (job_id TEXT PRIMARY KEY, researched_at TEXT NOT NULL, version INTEGER NOT NULL,
      salary_low INTEGER, salary_high INTEGER, currency TEXT, period TEXT, basis TEXT NOT NULL, ask_amount INTEGER,
      confidence TEXT NOT NULL, company_summary TEXT, company_website TEXT, sources_json TEXT NOT NULL DEFAULT '[]', payload_json TEXT NOT NULL)""")
    result = jobhunter_research.research(JOB, search=offline, fetch=lambda url: None, now=NOW)
    conn.execute("INSERT INTO job_research (job_id, researched_at, version, basis, confidence, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                 (result.job_id, result.researched_at, result.version, result.salary["basis"], "high", json.dumps(result.to_dict())))

    cache.ensure_schema(conn)

    assert conn.execute("SELECT job_id, basis FROM job_research").fetchone() == ("sdj-9", "published")
    assert conn.execute("SELECT context_sha256 FROM job_research WHERE job_id='sdj-9'").fetchone() == ("",)
    assert cache.load(conn, "sdj-9", 14, now=NOW, context_sha256=cache.context_digest(JOB)) is None


def test_context_edits_and_research_settings_invalidate_cache(conn):
    settings = {"target": {"amount": 70000}, "quantile": 0.75, "round_to": 1000}
    digest = cache.context_digest(JOB, settings)
    result = jobhunter_research.research(JOB, search=offline, fetch=lambda url: None, now=NOW)
    cache.ensure_schema(conn)
    cache.store(conn, result, context_sha256=digest)

    for key, changed in (("title", "Tech Lead"), ("company", "OtherCo"), ("location", "Barcelona, Spain"),
                         ("description", "Updated responsibilities"), ("salary", "€80k–90k"),
                         ("min_experience", 8)):
        edited = dict(JOB, **{key: changed})
        assert cache.load(conn, "sdj-9", 14, now=NOW, context_sha256=cache.context_digest(edited, settings)) is None
    changed_settings = {**settings, "quantile": 0.9}
    assert cache.load(conn, "sdj-9", 14, now=NOW, context_sha256=cache.context_digest(JOB, changed_settings)) is None


def test_fresh_strong_cache_reuses_with_exact_context_for_fourteen_days(conn):
    from jobhunter_interest_flow import salary_target
    from jobhunter_research import ResearchResult

    context = {"target": salary_target(JOB), "quantile": 0.75, "round_to": 1000}
    digest = cache.context_digest(JOB, context)
    result = ResearchResult(
        job_id="sdj-9", researched_at=NOW.isoformat().replace("+00:00", "Z"),
        salary={"low": 60000, "high": 70000, "currency": "EUR", "period": "year", "basis": "published",
                "ask_amount": 68000, "published_text": "€60k–70k / year", "confidence": "high",
                "ask_basis": "published", "compensation_basis": "unspecified", "proxy_for_level": None},
        company={"name": "Acme", "website": "https://acme.es", "summary": "", "industry": "", "size": "",
                 "hq": "", "sources": []}, notes=[],
    )
    cache.ensure_schema(conn)
    cache.store(conn, result, context_sha256=digest)

    def must_not_search(query, **kwargs):
        raise AssertionError("valid strong result should be reused from the cache")

    reused = jobhunter_research.research(JOB, conn=conn, search=must_not_search, now=NOW + timedelta(days=13))
    assert reused.to_dict() == result.to_dict()


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
