import sqlite3

import scraper

CONFIG_BACKUP = dict(scraper.CONFIG)


def make_conn(rows):
    """An in-memory jobs table seeded with the given (id, overrides) rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            score INTEGER,
            notified INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            ai_verdict TEXT DEFAULT '',
            ai_verdict_reason TEXT DEFAULT '',
            ai_sponsorship TEXT DEFAULT '',
            ai_rank INTEGER
        )
        """
    )
    for job_id, over in rows:
        base = {
            "id": job_id, "title": "Backend Architect", "company": "Acme",
            "location": "Dubai, United Arab Emirates", "score": 60,
            "notified": 0, "status": "new",
        }
        base.update(over)
        conn.execute(
            "INSERT INTO jobs (id, title, company, location, score, notified, status) "
            "VALUES (:id, :title, :company, :location, :score, :notified, :status)",
            base,
        )
    conn.commit()
    return conn


def verdict(job_id, verdict, **over):
    base = {"job_id": job_id, "verdict": verdict, "reason": "test reason",
            "sponsorship": "implied"}
    if verdict == "send":
        base["rank"] = 1
    base.update(over)
    return base


def setup_module(module):
    scraper.CONFIG["score_threshold"] = 45


def teardown_module(module):
    scraper.CONFIG.clear()
    scraper.CONFIG.update(CONFIG_BACKUP)


def test_record_review_writes_a_send_verdict_and_returns_it():
    conn = make_conn([("j1", {})])
    written = scraper.record_review(conn, [verdict("j1", "send", rank=2)])

    row = conn.execute("SELECT * FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == "send"
    assert row["ai_verdict_reason"] == "test reason"
    assert row["ai_sponsorship"] == "implied"
    assert row["ai_rank"] == 2
    assert row["status"] == "new"
    assert len(written) == 1
    assert written[0]["market"] == "dubai"


def test_record_review_sets_status_rejected_for_a_reject_verdict():
    conn = make_conn([("j1", {})])
    scraper.record_review(conn, [verdict("j1", "reject")])

    row = conn.execute("SELECT status, ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["status"] == "rejected"
    assert row["ai_verdict"] == "reject"


def test_record_review_leaves_a_hold_verdict_as_status_new_not_notified():
    conn = make_conn([("j1", {})])
    scraper.record_review(conn, [verdict("j1", "hold")])

    row = conn.execute("SELECT status, notified, ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["status"] == "new"
    assert row["notified"] == 0
    assert row["ai_verdict"] == "hold"


def test_record_review_skips_a_job_id_not_in_todays_eligible_candidates():
    # already notified -- not an eligible candidate today
    conn = make_conn([("j1", {"notified": 1})])
    written = scraper.record_review(conn, [verdict("j1", "send")])

    row = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == ""
    assert written == []


def test_record_review_skips_an_unknown_job_id():
    conn = make_conn([])
    written = scraper.record_review(conn, [verdict("does-not-exist", "send")])
    assert written == []


def test_record_review_is_a_no_op_on_an_empty_batch():
    conn = make_conn([("j1", {})])
    written = scraper.record_review(conn, [])
    assert written == []
    row = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == ""


def test_record_review_skips_an_invalid_verdict_or_sponsorship_value():
    conn = make_conn([("j1", {}), ("j2", {})])
    written = scraper.record_review(conn, [
        verdict("j1", "definitely-maybe"),
        verdict("j2", "send", sponsorship="probably"),
    ])
    row1 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    row2 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j2'").fetchone()
    assert row1["ai_verdict"] == ""
    assert row2["ai_verdict"] == ""
    assert written == []


def test_record_review_rejects_the_whole_batch_on_a_duplicate_job_id():
    conn = make_conn([("j1", {})])
    written = scraper.record_review(conn, [
        verdict("j1", "send", rank=1),
        verdict("j1", "hold"),
    ])
    row = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == ""
    assert written == []


def test_record_review_rejects_the_whole_batch_on_a_duplicate_rank_among_sends():
    conn = make_conn([("j1", {}), ("j2", {})])
    written = scraper.record_review(conn, [
        verdict("j1", "send", rank=1),
        verdict("j2", "send", rank=1),
    ])
    row1 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    row2 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j2'").fetchone()
    assert row1["ai_verdict"] == ""
    assert row2["ai_verdict"] == ""
    assert written == []


def test_record_review_truncates_a_reason_over_ten_words_instead_of_rejecting():
    conn = make_conn([("j1", {})])
    long_reason = " ".join(f"word{i}" for i in range(20))
    scraper.record_review(conn, [verdict("j1", "send", reason=long_reason)])

    row = conn.execute("SELECT ai_verdict_reason FROM jobs WHERE id = 'j1'").fetchone()
    assert len(row["ai_verdict_reason"].split()) == 10


def test_record_review_never_touches_score():
    conn = make_conn([("j1", {"score": 77})])
    scraper.record_review(conn, [verdict("j1", "send")])
    row = conn.execute("SELECT score FROM jobs WHERE id = 'j1'").fetchone()
    assert row["score"] == 77
