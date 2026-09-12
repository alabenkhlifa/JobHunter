"""Visa-sponsorship fast lane: stored pre-read, relaxed threshold, verified quotes."""
from datetime import datetime, timezone

import pytest

import scraper

CONFIG_BACKUP = dict(scraper.CONFIG)
NOW = datetime.now(timezone.utc)
OFFER = "Benefits include visa sponsorship, flights and 2 weeks paid accommodation."
SILENT = "Build scalable Java services on AWS with a senior backend team."


@pytest.fixture
def conn(tmp_path):
    scraper.CONFIG["db_path"] = str(tmp_path / "jobs.db")
    scraper.CONFIG["score_threshold"] = 45
    scraper.CONFIG["sponsored_score_threshold"] = 35
    scraper.CONFIG["review_rubric"] = "test-rubric"
    connection = scraper.init_db()
    connection.row_factory = __import__("sqlite3").Row
    yield connection
    connection.close()
    scraper.CONFIG.clear()
    scraper.CONFIG.update(CONFIG_BACKUP)


def insert(conn, job_id, *, score=60, description=SILENT, **over):
    job = {"id": job_id, "title": "Backend Architect", "company": "Acme",
           "location": "Dubai, United Arab Emirates", "url": f"https://example.com/{job_id}",
           "source": "LinkedIn", "score": score, "description": description}
    job.update(over)
    scraper.save_job(conn, job)
    return job


def test_save_job_stores_the_sponsorship_pre_read(conn):
    insert(conn, "offer", description=OFFER)
    insert(conn, "silent")
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}
    assert rows["offer"]["sponsorship_signal"] == "offered"
    assert "visa sponsorship" in rows["offer"]["sponsorship_evidence"]
    assert rows["silent"]["sponsorship_signal"] == ""
    assert rows["silent"]["sponsorship_evidence"] == ""


def test_backfill_fills_only_rows_without_a_pre_read(conn):
    insert(conn, "offer", description=OFFER)
    insert(conn, "old", description=OFFER)
    conn.execute("UPDATE jobs SET sponsorship_signal = NULL, sponsorship_evidence = NULL WHERE id = 'old'")
    conn.execute("UPDATE jobs SET sponsorship_signal = 'excluded', sponsorship_evidence = 'kept' WHERE id = 'offer'")
    conn.commit()
    report = scraper.backfill_sponsorship(conn)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}
    assert rows["old"]["sponsorship_signal"] == "offered"
    assert rows["offer"]["sponsorship_evidence"] == "kept"
    assert report == {"scanned": 1, "offered": 1, "excluded": 0}


def test_an_offer_passes_review_at_the_relaxed_threshold(conn):
    insert(conn, "offer-40", score=40, description=OFFER)
    insert(conn, "silent-40", score=40)
    insert(conn, "offer-30", score=30, description=OFFER)
    insert(conn, "silent-60", score=60)
    assert {j["id"] for j in scraper.get_review_candidates(conn, now=NOW)} == {"offer-40", "silent-60"}


def test_a_stated_refusal_is_ineligible(conn):
    insert(conn, "refused", description="We are unable to sponsor work visas for this role.")
    assert scraper.get_review_candidates(conn, now=NOW) == []


def test_review_candidates_carry_the_evidence(conn):
    insert(conn, "offer", description=OFFER)
    [job] = scraper.get_review_candidates(conn, now=NOW)
    assert job["sponsorship_signal"] == "offered"
    assert "visa sponsorship" in job["sponsorship_evidence"]


def verdict(job_id, **over):
    base = {"job_id": job_id, "verdict": "send", "reason": "fits", "sponsorship": "no_info", "rank": 1}
    base.update(over)
    return base


def test_offered_needs_a_quote_that_exists_in_the_description(conn):
    insert(conn, "real", description=OFFER)
    insert(conn, "fake", description=SILENT)
    report = []
    scraper.record_review(conn, [
        verdict("real", sponsorship="offered", evidence="visa sponsorship, flights", rank=1),
        verdict("fake", sponsorship="offered", evidence="we sponsor your visa", rank=2),
    ], report=report)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}
    assert rows["real"]["ai_sponsorship"] == "offered"
    assert rows["fake"]["ai_sponsorship"] == "no_info"
    assert [note["job_id"] for note in report] == ["fake"]
    assert "quote" in report[0]["note"]


def test_offered_without_any_quote_is_downgraded_and_reported(conn):
    insert(conn, "bare", description=OFFER)
    report = []
    scraper.record_review(conn, [verdict("bare", sponsorship="offered")], report=report)
    assert conn.execute("SELECT ai_sponsorship FROM jobs WHERE id='bare'").fetchone()[0] == "no_info"
    assert report and report[0]["job_id"] == "bare"


@pytest.mark.parametrize("legacy", ["implied", "doubtful"])
def test_legacy_reads_normalise_to_no_info_with_a_note(conn, legacy):
    insert(conn, "old")
    report = []
    scraper.record_review(conn, [verdict("old", sponsorship=legacy)], report=report)
    assert conn.execute("SELECT ai_sponsorship FROM jobs WHERE id='old'").fetchone()[0] == "no_info"
    assert report[0]["job_id"] == "old" and legacy in report[0]["note"]


def test_an_unknown_job_or_verdict_is_reported_not_silently_dropped(conn):
    insert(conn, "known")
    report = []
    scraper.record_review(conn, [verdict("ghost"), verdict("known", verdict="maybe")], report=report)
    assert {note["job_id"] for note in report} == {"ghost", "known"}


def test_record_review_stamps_time_and_rubric(conn):
    insert(conn, "j")
    scraper.record_review(conn, [verdict("j", verdict="hold")])
    row = dict(conn.execute("SELECT * FROM jobs WHERE id='j'").fetchone())
    assert row["ai_rubric"] == "test-rubric"
    assert datetime.fromisoformat(row["ai_reviewed_at"]) > NOW.replace(microsecond=0)


def test_skip_job_records_the_reason_and_leaves_the_queue(conn):
    insert(conn, "bad")
    assert scraper.skip_job(conn, "bad", "too senior") is True
    assert scraper.skip_job(conn, "missing", "x") is False
    row = conn.execute("SELECT status FROM jobs WHERE id='bad'").fetchone()
    assert row[0] == "skipped"
    feedback = conn.execute("SELECT action, reason, source FROM job_feedback").fetchone()
    assert tuple(feedback) == ("skip", "too senior", "telegram_text")


def test_feedback_examples_are_recent_deduped_and_carry_job_facts(conn):
    insert(conn, "liked", title="Tech Lead, Java", tech_required="java, spring", min_experience=5)
    insert(conn, "hated", title="Backend Developer (.NET)", tech_required="c#, .net")
    insert(conn, "cta", title="CTA Final Wrong Stack Test")
    scraper.record_job_feedback(conn, "liked", "interested", reason="user selected interested")
    scraper.record_job_feedback(conn, "liked", "interested", reason="user selected interested")
    scraper.record_job_feedback(conn, "cta", "skip", reason="wrong stack")
    scraper.record_job_feedback(conn, "hated", "skip", reason="wrong stack, .NET")
    examples = scraper.get_feedback_examples(conn, limit=10)
    assert [e["id"] for e in examples] == ["hated", "liked"]
    assert examples[0] == {
        "id": "hated", "action": "skip", "reason": "wrong stack, .NET",
        "title": "Backend Developer (.NET)", "company": "Acme",
        "location": "Dubai, United Arab Emirates", "tech_required": "c#, .net",
        "min_experience": -1, "score": 60, "date": examples[0]["date"],
    }
    assert scraper.get_feedback_examples(conn, limit=1) == examples[:1]


@pytest.mark.parametrize("sponsorship,evidence,expected_market", [
    ("offered", "", "dubai"),
    ("offered", "invented offer", "dubai"),
    ("offered", OFFER, "jeddah"),
    ("implied", "", "dubai"),
    ("doubtful", "", "dubai"),
])
def test_plan_and_record_select_the_same_market(conn, monkeypatch, sponsorship, evidence, expected_market):
    import jobhunter_queue
    monkeypatch.setitem(scraper.CONFIG, "delivery", {"per_market": 3, "cap": 1})
    insert(conn, "strong", score=90)
    insert(conn, "visa", location="Jeddah", score=60, description=OFFER)
    verdicts = [verdict("strong", rank=1), verdict("visa", sponsorship=sponsorship, evidence=evidence, rank=2)]
    plan_notes, record_notes = [], []
    before = conn.total_changes
    plan = scraper.plan_reviewed_digest(conn, verdicts, report=plan_notes)
    assert conn.total_changes == before
    written = scraper.record_review(conn, verdicts, report=record_notes)
    selected = jobhunter_queue.select_ranked(scraper.reviewed_queue(conn, written), cap=1)
    assert selected[0]["market"] == expected_market
    assert plan["markets"][expected_market]["selected"] == 1
    assert plan_notes == record_notes


def test_read_only_feedback_without_tables_returns_no_examples(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs (id TEXT)")
    before = path.read_bytes()
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        assert scraper.get_feedback_examples(connection, initialize=False) == []
    assert path.read_bytes() == before


def test_owner_review_preferences_include_spoken_languages():
    assert scraper.DEFAULT_CONFIG["review_preferences"]["languages"] == ["French", "English", "Arabic"]


def test_legacy_offer_without_evidence_does_not_get_visa_priority(conn):
    import jobhunter_queue
    insert(conn, "legacy", score=60)
    insert(conn, "strong", score=90)
    conn.execute("UPDATE jobs SET ai_verdict='send', ai_sponsorship='offered', ai_rank=1 WHERE id='legacy'")
    conn.execute("UPDATE jobs SET ai_verdict='send', ai_sponsorship='no_info', ai_rank=2 WHERE id='strong'")
    conn.commit()
    candidates = scraper.get_review_candidates(conn, now=NOW)
    assert next(j for j in candidates if j["id"] == "legacy")["ai_sponsorship"] == "no_info"
    assert jobhunter_queue.select_reviewed_queue(candidates, cap=1)[0]["id"] == "strong"


def test_generic_plan_on_read_only_db_does_not_create_context_table(conn, monkeypatch):
    import sqlite3
    from pathlib import Path
    monkeypatch.setitem(scraper.CONFIG, "matching", {"preset": "generic", "preferred_roles": ["architect"]})
    monkeypatch.setitem(scraper.CONFIG, "markets", [{"name": "Dubai", "locations": ["Dubai"], "work_authorization": "authorized", "relocation_required": False}])
    insert(conn, "role")
    before = Path(scraper.CONFIG["db_path"]).read_bytes()
    with sqlite3.connect(f"{Path(scraper.CONFIG['db_path']).as_uri()}?mode=ro", uri=True) as read_only:
        scraper.plan_reviewed_digest(read_only)
    assert Path(scraper.CONFIG["db_path"]).read_bytes() == before
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobhunter_review_context'").fetchone()


def test_verified_offer_below_regular_threshold_survives_record_and_delivery_selection(conn, monkeypatch):
    import jobhunter_queue
    monkeypatch.setitem(scraper.CONFIG, "delivery", {"per_market": 3, "cap": 1})
    insert(conn, "strong", score=90)
    insert(conn, "visa", score=40, location="Jeddah", description=OFFER)
    verdicts = [verdict("strong", rank=1), verdict("visa", sponsorship="offered", evidence=OFFER, rank=2)]
    plan = scraper.plan_reviewed_digest(conn, verdicts)
    written = scraper.record_review(conn, verdicts)
    selected = jobhunter_queue.select_ranked(scraper.reviewed_queue(conn, written), cap=1)
    assert [job["id"] for job in selected] == ["visa"]
    assert selected[0]["score"] == 40
    assert plan["markets"]["jeddah"]["selected"] == 1
