import sqlite3
from datetime import datetime, timezone
from unittest import mock

import scraper


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
    assert msg.count("nothing today") == 5
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
    assert lines[dubai_idx].startswith("1️⃣")
    assert lines[ch_idx].startswith("2️⃣")
    assert dubai_idx < ch_idx


def test_format_digest_message_sorts_within_a_market_by_ai_rank():
    # Two jobs in the SAME market, supplied worst-rank-first: the digest must
    # print them in ai_rank order regardless of the order they arrive in.
    jobs = [
        job(id="dx2", market="dubai", ai_rank=2, title="Runner Up"),
        job(id="dx1", market="dubai", ai_rank=1, title="Top Pick"),
    ]
    msg = scraper.format_digest_message(jobs, 0, [])
    lines = msg.splitlines()
    top_idx = next(i for i, l in enumerate(lines) if "Top Pick" in l)
    runner_idx = next(i for i, l in enumerate(lines) if "Runner Up" in l)
    assert top_idx < runner_idx
    assert lines[top_idx].startswith("1️⃣")
    assert lines[runner_idx].startswith("2️⃣")


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
    assert "hires directly" in scraper.format_digest_message([direct_job], 0, [])
    assert "via a recruiter" in scraper.format_digest_message([agency_job], 0, [])


def test_format_digest_message_shows_sponsorship_read():
    msg = scraper.format_digest_message([job(ai_sponsorship="offered")], 0, [])
    assert "sponsorship offered" in msg


def test_format_digest_message_escapes_html_in_job_text():
    # send_telegram posts with parse_mode=HTML, and the digest is one message:
    # a single stray "<" or "&" in any job would cost the whole night's send,
    # not just the one card it came from.
    msg = scraper.format_digest_message(
        [
            job(
                title="Front <End> Dev",
                company="AT&T",
                tech_required="C++ & <script>",
                ai_verdict_reason="pays > market",
                ai_sponsorship="offered <confirmed>",
            )
        ],
        0,
        [],
    )
    assert "Front &lt;End&gt; Dev" in msg
    assert "AT&amp;T" in msg
    assert "C++ &amp; &lt;script&gt;" in msg
    assert "pays &gt; market" in msg
    assert "offered &lt;confirmed&gt;" in msg
    assert "<" not in msg
    assert ">" not in msg


def test_format_digest_message_shows_the_queued_line():
    msg = scraper.format_digest_message([], 6, [71, 68, 66])
    assert "6 more queued" in msg
    assert "71, 68, 66" in msg


def test_format_digest_message_omits_queued_line_when_nothing_queued():
    msg = scraper.format_digest_message([], 0, [])
    assert "more queued" not in msg


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
            ai_sponsorship TEXT DEFAULT '', ai_rank INTEGER
        )
        """
    )
    for job_id, over in rows:
        base = {"id": job_id, "title": "Backend Architect", "company": "Acme",
                "location": "Dubai, United Arab Emirates", "score": 60,
                "notified": 0, "status": "new", "ai_rank": None}
        base.update(over)
        conn.execute(
            "INSERT INTO jobs (id, title, company, location, score, notified, status, ai_rank) "
            "VALUES (:id, :title, :company, :location, :score, :notified, :status, :ai_rank)",
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
