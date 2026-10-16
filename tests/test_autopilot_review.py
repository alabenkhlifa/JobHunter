import pytest

import scraper
from jobhunter_autopilot import review, store
from test_autopilot_helpers import NOW, RUBRIC, RUN_ID, add_job, enabled_policy, make_db


@pytest.fixture
def conn(tmp_path):
    connection = make_db(tmp_path / "jobs.db")
    yield connection
    connection.close()


def test_envelope_matches_the_collector_shape(conn):
    unreviewed = add_job(conn, ai_verdict="", ai_rubric="", status="archived",
                         date_posted="2026-07-01", date_scraped="2026-07-01T20:00:00+00:00")
    old_hold = add_job(conn, ai_verdict="hold", ai_reviewed_at="2026-10-01T10:00:00+00:00")
    add_job(conn)  # already a current send: nothing to review
    envelope = review.review_batch(conn, enabled_policy(), NOW)
    assert envelope["count"] == 2
    assert envelope["review_constraints"] == {"max_experience": scraper.CONFIG["max_experience"],
                                              "max_job_age_days": None}
    assert envelope["review_preferences"] == scraper.CONFIG["review_preferences"]
    assert envelope["feedback_examples"] == []
    by_id = {item["id"]: item for item in envelope["candidates"]}
    assert set(by_id) == {unreviewed["id"], old_hold["id"]}
    for item in by_id.values():
        assert set(review.FIELD_NAMES) <= set(item)
        assert item["effective_score"] == 88
    assert by_id[old_hold["id"]]["previous_verdict"] == "hold"
    assert by_id[old_hold["id"]]["ai_verdict"] == ""
    assert by_id[unreviewed["id"]]["previous_verdict"] is None
    auto = envelope["autopilot"]
    assert auto["run_id"] == RUN_ID and auto["enabled"] is True
    assert auto["needs_review"] == {unreviewed["id"]: "unreviewed", old_hold["id"]: "hold_expired"}
    assert auto["policy_summary"].startswith("Auto apply: ON")


def test_batch_limit_and_disabled_noop(conn):
    for _ in range(3):
        add_job(conn, ai_verdict="")
    limited = review.review_batch(conn, enabled_policy(review_batch_size=2), NOW)
    assert limited["count"] == 2 and limited["autopilot"]["pending_beyond_batch"] == 1
    assert review.review_batch(conn, enabled_policy(), NOW, limit=1)["count"] == 1
    disabled = enabled_policy()
    disabled["enabled"] = False
    assert review.review_batch(conn, disabled, NOW)["count"] == 0
    paused = enabled_policy(paused=True)
    assert review.review_batch(conn, paused, NOW)["candidates"] == []


def test_record_review_accepts_old_archived_jobs_and_rejects(conn):
    archived = add_job(conn, ai_verdict="", ai_rubric="", status="archived",
                       date_posted="2026-06-01", date_scraped="2026-06-01T20:00:00+00:00")
    other = add_job(conn, ai_verdict="", ai_rubric="")
    outsider = add_job(conn, title="Junior Developer", ai_verdict="")
    pool, _ = review.review_pool(conn, enabled_policy(), now=NOW)
    # scraper.record_review would drop the archived/old one: prove the autopilot keeps it.
    assert archived["id"] not in {row["id"] for row in scraper.get_review_candidates(conn)}
    verdicts = [
        {"job_id": archived["id"], "verdict": "send", "reason": "good java fit", "sponsorship": "no_info", "rank": 1},
        {"job_id": other["id"], "verdict": "reject", "reason": "dotnet", "sponsorship": "no_info"},
        {"job_id": outsider["id"], "verdict": "send", "reason": "x", "sponsorship": "no_info", "rank": 2},
    ]
    result = review.record_review(conn, verdicts, pool, now=NOW)
    assert result["recorded"] == 2 and result["send"] == 1 and result["reject"] == 1
    assert [note["job_id"] for note in result["review_notes"]] == [outsider["id"]]
    row = conn.execute("SELECT ai_verdict, ai_rubric, status, ai_rank, ai_reviewed_at FROM jobs WHERE id=?",
                       (archived["id"],)).fetchone()
    assert tuple(row) == ("send", RUBRIC, "archived", 1, NOW.isoformat())
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (other["id"],)).fetchone()[0] == "rejected"


def test_record_review_refuses_a_broken_batch(conn):
    a = add_job(conn, ai_verdict="")
    b = add_job(conn, ai_verdict="")
    pool, _ = review.review_pool(conn, enabled_policy(), now=NOW)
    verdicts = [{"job_id": a["id"], "verdict": "send", "sponsorship": "no_info", "rank": 1},
                {"job_id": b["id"], "verdict": "send", "sponsorship": "no_info", "rank": 1}]
    result = review.record_review(conn, verdicts, pool, now=NOW)
    assert result["recorded"] == 0 and "batch refused" in result["review_notes"][0]["note"]


def test_todays_logged_jobs_leave_the_pool(conn):
    job = add_job(conn, ai_verdict="")
    store.ensure_schema(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "blocked")
    assert review.review_batch(conn, enabled_policy(), NOW)["count"] == 0


def test_record_review_works_with_a_scraper_that_lacks_preserve_doubtful(conn, monkeypatch):
    """The deployed scraper may predate the preserve_doubtful keyword."""
    original = scraper.validated_verdicts

    def older(verdicts, eligible, *, report=None, descriptions=None, verified_evidence=None):
        return original(verdicts, eligible, report=report, descriptions=descriptions,
                        verified_evidence=verified_evidence)

    monkeypatch.setattr(scraper, "validated_verdicts", older)
    job = add_job(conn, status="archived", date_scraped="2026-06-01T20:00:00+00:00", ai_verdict="")
    pool, _ = review.review_pool(conn, enabled_policy(), now=NOW)
    verdicts = [{"job_id": job["id"], "verdict": "send", "reason": "fits", "sponsorship": "no_info", "rank": 1}]
    result = review.record_review(conn, verdicts, pool, now=NOW)
    assert result["recorded"] == 1 and result["send"] == 1
