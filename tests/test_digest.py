import sqlite3
from datetime import datetime, timezone
from unittest import mock

import pytest
from bs4 import BeautifulSoup

import scraper


@pytest.fixture(autouse=True)
def verified_listing_for_digest_unit_tests(monkeypatch):
    # Rendering/Telegram acknowledgement tests isolate the new network gate.
    # Real source parsing and persisted gate outcomes live in test_pre_delivery.
    monkeypatch.setattr('jobhunter_delivery.revalidate', lambda conn, job: {'state': 'open', 'matched': True})


def job(**over):
    base = {
        "id": "j1", "title": "Backend Lead", "company": "Acme",
        "score": 80, "market": "dubai", "ai_rank": 1,
        "ai_sponsorship": "implied", "ai_verdict_reason": "solid fit",
        "tech_required": "Java, Spring", "date_posted": "", "location": "Dubai",
        "recruiter_company": "", "credibility_notes": "",
    }
    base.update(over)
    return base


def test_format_digest_message_shows_every_market_when_empty():
    msg = scraper.format_digest_message([], 0, [])
    assert "No matches: Dubai, Abu Dhabi, Jeddah, Riyadh, Switzerland" in msg
    assert "0 sent" in msg


def test_format_digest_message_numbers_by_display_order_not_ai_rank():
    # A Dubai job with a WORSE (higher) ai_rank than a Switzerland job must
    # still be numbered 1, because Dubai prints first in DIGEST_MARKET_ORDER.
    jobs = [
        job(id="ch1", market="switzerland", ai_rank=1, title="CH Job"),
        job(id="dx1", market="dubai", ai_rank=5, title="Dubai Job"),
    ]
    msg = scraper.format_digest_message(jobs, 0, [])
    lines = msg.splitlines()
    dubai_idx = next(i for i, l in enumerate(lines) if "Dubai Job" in l)
    ch_idx = next(i for i, l in enumerate(lines) if "CH Job" in l)
    assert lines[dubai_idx].startswith("<b>1.")
    assert lines[ch_idx].startswith("<b>2.")
    assert dubai_idx < ch_idx


def test_format_digest_message_sorts_within_a_market_by_score_descending():
    # Display follows score even when the AI ranked the lower-score job first.
    jobs = [
        job(id="dx2", market="dubai", score=73, ai_rank=1, title="Runner Up"),
        job(id="dx1", market="dubai", score=84, ai_rank=2, title="Top Pick"),
    ]
    msg = scraper.format_digest_message(jobs, 0, [])
    lines = msg.splitlines()
    top_idx = next(i for i, l in enumerate(lines) if "Top Pick" in l)
    runner_idx = next(i for i, l in enumerate(lines) if "Runner Up" in l)
    assert top_idx < runner_idx
    assert lines[top_idx].startswith("<b>1.")
    assert lines[runner_idx].startswith("<b>2.")


def test_score_ties_use_ai_rank_then_id_without_changing_input():
    jobs = [
        job(id="b", ai_rank=2, title="Third"),
        job(id="a", ai_rank=2, title="Second"),
        job(id="c", ai_rank=1, title="First"),
    ]
    original = [dict(j) for j in jobs]
    msg = scraper.format_digest_message(jobs, 0, [])
    assert msg.index("First") < msg.index("Second") < msg.index("Third")
    assert jobs == original


def test_format_digest_message_survives_null_job_fields():
    # The jobs table has no NOT NULL on title, company or ai_sponsorship, so a
    # row can hold SQL NULL. dict.get's default only fires on a MISSING key,
    # never on a present-but-None value -- and one None would take down the
    # whole night's digest, not just the entry it came from.
    nulled = job(title=None, company=None, ai_sponsorship=None,
                 tech_required=None, ai_verdict_reason=None)
    msg = scraper.format_digest_message([nulled], 0, [])
    assert "1 sent" in msg
    assert "None" not in msg


def test_format_digest_message_shows_hiring_route_for_both_employer_tiers():
    direct_job = job(id="d1", company="Acme")
    agency_job = job(id="a1", company="Confidential Recruitment Agency")
    assert "recruiter" not in scraper.format_digest_message([direct_job], 0, [])
    assert "· recruiter" in scraper.format_digest_message([agency_job], 0, [])


def test_format_digest_message_shows_sponsorship_read():
    msg = scraper.format_digest_message([job(ai_sponsorship="offered")], 0, [])
    assert "<b>✅ Visa offered</b>" in msg


def test_digest_distinguishes_a_silent_posting_from_an_inferred_one():
    # no_info is the 97.7% case and it sends, so the reader has to be able to
    # tell "the listing did not say" from "the review inferred it".
    msg = scraper.format_digest_message([job(ai_sponsorship="no_info")], 0, [])
    assert "<b>🛂 Visa not mentioned</b>" in msg


def test_digest_bolds_company_and_unconfirmed_visa_and_empty_markets():
    msg = scraper.format_digest_message([job(company="AT&T")], 0, [])
    assert "<b>AT&amp;T</b>" in msg
    assert "<b>❓ Visa unconfirmed</b>" in msg
    assert "<b>⚠️ No matches: Abu Dhabi, Jeddah, Riyadh, Switzerland</b>" in msg


@pytest.mark.parametrize("score,icon", [(0, "👍"), (69, "👍"), (70, "⭐"), (79, "⭐"), (80, "🔥"), (100, "🔥")])
def test_digest_score_icons_cover_tier_boundaries(score, icon):
    msg = scraper.format_digest_message([job(score=score)], 0, [])
    assert f"{icon} {score}/100 ·" in msg


def test_format_digest_message_escapes_html_and_links_in_job_text():
    url = 'https://example.test/jobs?id=1&ref="email"'
    msg = scraper.format_digest_message([job(
        title="Front <End> Dev", company="AT&T", url=url,
        ai_sponsorship="offered <confirmed>",
    )], 0, [])
    assert "Front &lt;End&gt; Dev" in msg
    assert "AT&amp;T" in msg
    assert 'ref=&quot;email&quot;' in msg
    soup = BeautifulSoup(msg, "html.parser")
    assert set(tag.name for tag in soup.find_all()) == {"b", "a"}
    assert soup.a["href"] == url
    assert soup.a.get_text() == "Front <End> Dev"
    assert "Visa unknown" in msg


def test_format_digest_message_shows_queue_count_only_once():
    msg = scraper.format_digest_message([], 6, [71, 68, 66])
    assert msg.count("6 queued") == 1
    assert "71, 68, 66" not in msg


def test_format_digest_message_omits_repeated_queue_footer():
    msg = scraper.format_digest_message([], 0, [])
    assert "more queued" not in msg


def test_digest_omits_long_keyword_lists_and_review_prose():
    msg = scraper.format_digest_message([job(
        tech_required="kubernetes, kafka, java" * 100,
        ai_verdict_reason="Lengthy detailed review rationale",
    )], 0, [])
    assert "kubernetes" not in msg
    assert "Lengthy detailed review rationale" not in msg
    assert "🔥 80/100 · <b>❓ Visa unconfirmed</b>" in msg


@pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "file:///etc/passwd", "https://[invalid"])
def test_digest_renders_plain_title_when_link_is_missing_or_invalid(url):
    msg = scraper.format_digest_message([job(url=url)], 0, [])
    assert "Backend Lead" in msg
    assert "<a " not in msg


def test_digest_twelve_long_jobs_fit_telegram_limit_without_losing_any():
    jobs = [job(
        id=f"j{n}", ai_rank=n, market=scraper.DIGEST_MARKET_ORDER[(n - 1) % 5],
        title="Long job title 🧩 " * 100, company="Long company name 🏢 " * 100,
        url=f"https://example.test/job/{n}?ref=telegram&source=test",
        tech_required="framework " * 1000, ai_verdict_reason="reason " * 1000,
    ) for n in range(1, 13)]
    msg = scraper.format_digest_message(jobs, 103, [80, 70, 60])
    soup = BeautifulSoup(msg, "html.parser")
    assert len(soup.find_all("a")) == 12
    # UTF-16 counting is conservative for Telegram's entity offsets.
    assert len(soup.get_text().encode("utf-16-le")) // 2 <= 4096
    assert "12 sent" in msg
    assert "12. " in soup.get_text()
    assert all(len(link.get_text()) <= 64 for link in soup.find_all("a"))


def test_digest_normalizes_embedded_newlines_and_preserves_zero_score():
    msg = scraper.format_digest_message([job(
        title="Backend\n\t Lead", company="Acme\nCompany", score=0,
    )], 0, [])
    assert "Backend Lead" in msg
    assert "Acme Company" in msg
    assert "0/100" in msg


def test_digest_uses_exact_age_instead_of_rounding_eight_days_to_a_week():
    msg = scraper.format_digest_message([job(date_posted="2026-08-31")], 0, [],
                                        today=datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert "<b>Acme</b> · 8d ago" in msg
    assert "week" not in msg


def test_digest_separates_adjacent_job_entries():
    msg = scraper.format_digest_message([job(id="one", ai_rank=1), job(id="two", ai_rank=2)], 0, [])
    assert "Visa unconfirmed</b>\n\n<b>2." in msg


CONFIG_BACKUP = dict(scraper.CONFIG)


def make_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            score INTEGER, notified INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
            date_posted TEXT DEFAULT '', tech_required TEXT DEFAULT '',
            recruiter_company TEXT DEFAULT '', credibility_notes TEXT DEFAULT '',
            ai_verdict TEXT DEFAULT '', ai_verdict_reason TEXT DEFAULT '',
            ai_sponsorship TEXT DEFAULT '', ai_rank INTEGER,
            date_scraped TEXT, description TEXT DEFAULT '', min_experience INTEGER DEFAULT -1
        )
        """
    )
    for job_id, over in rows:
        base = {"id": job_id, "title": "Backend Architect", "company": "Acme",
                "location": "Dubai, United Arab Emirates", "score": 60,
                "notified": 0, "status": "new", "ai_rank": None,
                "date_scraped": datetime.now(timezone.utc).isoformat()}
        base.update(over)
        conn.execute(
            "INSERT INTO jobs (id, title, company, location, score, notified, status, ai_rank, date_scraped) "
            "VALUES (:id, :title, :company, :location, :score, :notified, :status, :ai_rank, :date_scraped)",
            base,
        )
    conn.commit()
    return conn


def setup_module(module):
    scraper.CONFIG["score_threshold"] = 45


def teardown_module(module):
    scraper.CONFIG.clear()
    scraper.CONFIG.update(CONFIG_BACKUP)


def test_send_digest_sends_exactly_one_message():
    conn = make_conn([("sent1", {"score": 80, "ai_rank": 1}), ("sent2", {"score": 70, "ai_rank": 2})])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
                for jid in ("sent1", "sent2")]
    with mock.patch.object(scraper, "send_telegram") as fake_send:
        fake_send.return_value = True
        scraper.send_digest("tok", "chat", conn, selected)
    assert fake_send.call_count == 1


def test_send_digest_marks_only_the_selected_jobs_notified():
    conn = make_conn([
        ("sent1", {"score": 80, "ai_rank": 1}),
        ("queued1", {"score": 60}),
    ])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id = ?", ("sent1",)).fetchone())]
    with mock.patch.object(scraper, "send_telegram") as fake_send:
        fake_send.return_value = True
        scraper.send_digest("tok", "chat", conn, selected)
    sent_row = conn.execute("SELECT notified FROM jobs WHERE id='sent1'").fetchone()
    queued_row = conn.execute("SELECT notified FROM jobs WHERE id='queued1'").fetchone()
    assert sent_row["notified"] == 1
    assert queued_row["notified"] == 0


@pytest.mark.parametrize("response", [False, None])
def test_failed_digest_leaves_all_jobs_pending(response):
    conn = make_conn([("selected", {"score": 80}), ("queued", {"score": 60})])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id='selected'").fetchone())]
    before = [tuple(row) for row in conn.execute("SELECT * FROM jobs")]
    with mock.patch.object(scraper, "send_telegram", return_value=response):
        with pytest.raises(RuntimeError, match="jobs remain pending"):
            scraper.send_digest("tok", "chat", conn, selected)
    assert [tuple(row) for row in conn.execute("SELECT * FROM jobs")] == before


def test_digest_retry_marks_jobs_only_after_api_acknowledgement():
    conn = make_conn([("selected", {"score": 80}), ("queued", {"score": 60})])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id='selected'").fetchone())]
    failed = mock.Mock(status_code=200)
    failed.json.return_value = {"ok": False, "description": "send rejected"}
    accepted = mock.Mock(status_code=200)
    accepted.json.return_value = {"ok": True, "result": {"message_id": 123}}
    with mock.patch.object(scraper.requests, "post", side_effect=[failed, accepted]) as post:
        with pytest.raises(RuntimeError, match="jobs remain pending"):
            scraper.send_digest("tok", "chat", conn, selected)
        assert conn.execute("SELECT SUM(notified) FROM jobs").fetchone()[0] == 0
        scraper.send_digest("tok", "chat", conn, selected)
    assert post.call_count == 2
    assert dict(conn.execute("SELECT id, notified FROM jobs")) == {"selected": 1, "queued": 0}


def test_send_digest_queued_count_excludes_the_selected_jobs_and_ineligible_ones():
    conn = make_conn([
        ("sent1", {"score": 80, "ai_rank": 1}),
        ("queued1", {"score": 60}),
        ("queued2", {"score": 50}),
        ("below_threshold", {"score": 20}),
        ("already_notified", {"score": 90, "notified": 1}),
    ])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id = ?", ("sent1",)).fetchone())]
    with mock.patch.object(scraper, "format_digest_message") as fake_format:
        fake_format.return_value = "digest text"
        with mock.patch.object(scraper, "send_telegram") as fake_send:
            fake_send.return_value = True
            scraper.send_digest("tok", "chat", conn, selected)
    _, queued_count, queued_top_scores = fake_format.call_args[0]
    assert queued_count == 2
    assert queued_top_scores == [60, 50]


def test_list_queued_jobs_respects_limit_and_excludes_notified():
    conn = make_conn([
        ("q1", {"score": 90}),
        ("q2", {"score": 80}),
        ("q3", {"score": 70}),
        ("notified_already", {"score": 95, "notified": 1}),
    ])
    result = scraper.list_queued_jobs(conn, limit=2)
    assert [r["score"] for r in result] == [90, 80]


def test_list_queued_jobs_includes_market():
    conn = make_conn([("q1", {"score": 90, "location": "Zurich, Switzerland"})])
    result = scraper.list_queued_jobs(conn, limit=10)
    assert result[0]["market"] == "switzerland"


def test_list_queued_jobs_empty_queue_returns_empty_list():
    conn = make_conn([("below_threshold", {"score": 20})])
    assert scraper.list_queued_jobs(conn) == []
