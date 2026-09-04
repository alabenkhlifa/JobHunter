from datetime import datetime, timezone

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
