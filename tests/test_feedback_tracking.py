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


def test_job_inline_keyboard_opens_details_url_and_keeps_feedback_callbacks():
    keyboard = scraper.job_inline_keyboard({"id": "li-1", "url": "https://example.com"})

    buttons = keyboard["inline_keyboard"][0]
    assert buttons == [
        {"text": "✅ Interested", "callback_data": "interested:li-1"},
        {"text": "❌ Skip", "callback_data": "skip:li-1"},
        {"text": "📄 Details", "url": "https://example.com"},
    ]
    assert keyboard["inline_keyboard"][1] == [
        {"text": "Wrong stack", "callback_data": "skip_reason:wrong_stack:li-1"},
        {"text": "Too junior", "callback_data": "skip_reason:too_junior:li-1"},
        {"text": "Too senior", "callback_data": "skip_reason:too_senior:li-1"},
        {"text": "Low quality", "callback_data": "skip_reason:low_quality:li-1"},
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


def test_feedback_learning_demotes_jobs_matching_repeated_skip_reasons():
    summary = {"by_action": {"skip": 4}, "by_reason": {"wrong stack": 2, "too junior / low seniority": 1, "low-quality or suspicious posting": 1}}
    job = {
        "title": "Junior Frontend Developer",
        "company": "Unknown Staffing",
        "description": "Entry level frontend role with suspicious vague requirements.",
        "tech_required": "react, css",
        "tech_nice_to_have": "",
        "credibility_notes": "posted by agency/aggregator",
        "score": 10,
    }

    learned = scraper.apply_feedback_learning(job, summary)

    assert learned["feedback_adjustment"] < 0
    assert learned["feedback_adjusted_score"] < job["score"]
    assert "wrong_stack" in learned["feedback_learning_notes"]
    assert "too_junior" in learned["feedback_learning_notes"]
    assert "low_quality" in learned["feedback_learning_notes"]


def test_feedback_learning_boosts_jobs_similar_to_interested_backend_roles():
    summary = {"by_action": {"interested": 3}, "by_reason": {"strong backend fit": 2}}
    job = {
        "title": "Senior Backend Engineer",
        "company": "ExampleCo",
        "description": "Own backend APIs, architecture, and microservices.",
        "tech_required": "java, spring boot, microservices, aws",
        "tech_nice_to_have": "kubernetes",
        "credibility_notes": "",
        "score": 12,
    }

    learned = scraper.apply_feedback_learning(job, summary)

    assert learned["feedback_adjustment"] > 0
    assert learned["feedback_adjusted_score"] > job["score"]
    assert "interested_backend" in learned["feedback_learning_notes"]


def test_application_stage_tracks_linkedin_type_and_evidence():
    conn = make_conn_with_jobs()

    app_id = scraper.record_application_stage(
        conn,
        "li-1",
        "draft_ready",
        platform="LinkedIn",
        application_type="easy_apply",
        application_url="https://linkedin.com/jobs/view/1",
        evidence_path="data/output/li-1/screenshots/draft.png",
        notes="Ready for approval",
    )

    row = conn.execute(
        "SELECT id, stage, platform, application_type, application_url, evidence_path, notes FROM applications"
    ).fetchone()
    assert row == (
        app_id,
        "draft_ready",
        "LinkedIn",
        "easy_apply",
        "https://linkedin.com/jobs/view/1",
        "data/output/li-1/screenshots/draft.png",
        "Ready for approval",
    )


def test_application_answer_cache_reuses_only_confirmed_answers():
    conn = make_conn_with_jobs()

    scraper.cache_application_answer(conn, "Are you willing to relocate?", "Yes", confirmed=True)
    scraper.cache_application_answer(conn, "Expected salary?", "Ask user", confirmed=False)

    cached_relocation = scraper.get_cached_application_answer(conn, "Are you willing to relocate?")
    cached_salary = scraper.get_cached_application_answer(conn, "Expected salary?", confirmed_only=False)

    assert cached_relocation is not None
    assert cached_relocation["answer"] == "Yes"
    assert scraper.get_cached_application_answer(conn, "Expected salary?") is None
    assert cached_salary is not None
    assert cached_salary["answer"] == "Ask user"
