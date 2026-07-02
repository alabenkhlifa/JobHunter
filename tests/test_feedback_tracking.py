import sqlite3
from datetime import datetime, timezone

import scraper


def make_conn_with_jobs():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            source TEXT,
            score INTEGER,
            date_posted TEXT,
            date_scraped TEXT,
            notified INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            tech_required TEXT DEFAULT '',
            tech_nice_to_have TEXT DEFAULT '',
            min_experience INTEGER DEFAULT -1,
            salary TEXT DEFAULT '',
            work_model TEXT DEFAULT 'on-site',
            score_breakdown TEXT DEFAULT '',
            status TEXT DEFAULT 'new'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jobs (id, title, company, location, url, source, score, date_posted, date_scraped)
        VALUES ('li-1', 'Lead Backend Engineer', 'ExampleCo', 'Dubai', 'https://example.com', 'LinkedIn', 25, '', '')
        """
    )
    conn.commit()
    return conn


def test_job_inline_keyboard_uses_callback_for_details_so_we_can_learn_feedback():
    keyboard = scraper.job_inline_keyboard({"id": "li-1", "url": "https://example.com"})

    buttons = keyboard["inline_keyboard"][0]
    assert buttons == [
        {"text": "✅ Interested", "callback_data": "interested:li-1"},
        {"text": "❌ Skip", "callback_data": "skip:li-1"},
        {"text": "📄 Details", "callback_data": "details:li-1"},
    ]
    assert keyboard["inline_keyboard"][1] == [
        {"text": "Too junior", "callback_data": "skip_reason:too_junior:li-1"},
        {"text": "Wrong stack", "callback_data": "skip_reason:wrong_stack:li-1"},
        {"text": "Not Dubai", "callback_data": "skip_reason:not_dubai:li-1"},
        {"text": "Low quality", "callback_data": "skip_reason:low_quality:li-1"},
        {"text": "Duplicate", "callback_data": "skip_reason:duplicate:li-1"},
    ]


def test_record_job_feedback_creates_a_traceable_action_log():
    conn = make_conn_with_jobs()
    now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)

    feedback_id = scraper.record_job_feedback(
        conn,
        "li-1",
        "skip",
        reason="not backend enough",
        source="telegram_button",
        now=now,
    )

    assert feedback_id == 1
    row = conn.execute(
        "SELECT job_id, action, reason, source, created_at FROM job_feedback"
    ).fetchone()
    assert row == (
        "li-1",
        "skip",
        "not backend enough",
        "telegram_button",
        "2026-07-02T12:00:00+00:00",
    )


def test_feedback_summary_counts_actions_and_reasons():
    conn = make_conn_with_jobs()
    scraper.record_job_feedback(conn, "li-1", "skip", reason="wrong seniority")
    scraper.record_job_feedback(conn, "li-1", "skip", reason="wrong seniority")
    scraper.record_job_feedback(conn, "li-1", "interested", reason="strong backend fit")

    summary = scraper.get_feedback_summary(conn)

    assert summary == {
        "by_action": {"interested": 1, "skip": 2},
        "by_reason": {"strong backend fit": 1, "wrong seniority": 2},
    }
