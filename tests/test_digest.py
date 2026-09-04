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


def test_format_digest_message_shows_hiring_route_for_both_employer_tiers():
    direct_job = job(id="d1", company="Acme")
    agency_job = job(id="a1", company="Confidential Recruitment Agency")
    assert "hires directly" in scraper.format_digest_message([direct_job], 0, [])
    assert "via a recruiter" in scraper.format_digest_message([agency_job], 0, [])


def test_format_digest_message_shows_sponsorship_read():
    msg = scraper.format_digest_message([job(ai_sponsorship="offered")], 0, [])
    assert "sponsorship offered" in msg


def test_format_digest_message_shows_the_queued_line():
    msg = scraper.format_digest_message([], 6, [71, 68, 66])
    assert "6 more queued" in msg
    assert "71, 68, 66" in msg


def test_format_digest_message_omits_queued_line_when_nothing_queued():
    msg = scraper.format_digest_message([], 0, [])
    assert "more queued" not in msg
