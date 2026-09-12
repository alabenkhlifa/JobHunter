"""Promised-visa jobs lead the digest and are never displaced by the cap."""
from bs4 import BeautifulSoup

import jobhunter_queue as queue
import scraper

from test_queue_backfill import approved, ids


def test_select_ranked_keeps_offered_jobs_ahead_of_the_cap():
    rows = [approved(f"d{i}", ai_sponsorship="no_info", ai_rank=i + 1, score=90) for i in range(14)]
    rows += [approved(f"r{i}", "Riyadh", ai_sponsorship="no_info", ai_rank=15 + i, score=90) for i in range(4)]
    rows.append(approved("visa", "Riyadh", ai_sponsorship="offered", ai_rank=19, score=40))
    selected = queue.select_ranked(rows, per_market=3, cap=12)
    assert len(selected) == 12
    assert "visa" in ids(selected)


def test_select_ranked_never_exceeds_the_cap_with_offers():
    rows = [approved(f"v{i}", ai_sponsorship="offered", ai_rank=i + 1) for i in range(15)]
    assert len(queue.select_ranked(rows, per_market=3, cap=12)) == 12


def job(job_id, market="dubai", **over):
    base = {"id": job_id, "title": f"Role {job_id}", "company": f"Co {job_id}", "score": 70,
            "market": market, "location": market.title(), "ai_rank": 1, "ai_sponsorship": "no_info",
            "ai_verdict_reason": "fit", "tech_required": "", "date_posted": "",
            "recruiter_company": "", "credibility_notes": "", "url": f"https://x.example/{job_id}"}
    base.update(over)
    return base


def test_digest_opens_with_a_visa_section_and_does_not_repeat_those_jobs():
    sent = [job("plain"), job("visa", "riyadh", ai_sponsorship="offered", score=55),
            job("swiss", "switzerland")]
    text = scraper.format_digest_message(sent, 0, [])
    lines = BeautifulSoup(text, "html.parser").get_text().splitlines()
    heading = next(i for i, line in enumerate(lines) if "VISA SPONSORSHIP" in line)
    dubai = next(i for i, line in enumerate(lines) if "DUBAI" in line)
    assert heading < dubai
    assert lines[heading + 1].startswith("1. Role visa")
    assert "Riyadh" in lines[heading + 2]
    assert text.count("Role visa") == 1
    assert "No matches: Riyadh" not in text
    assert "No matches: Abu Dhabi, Jeddah" in text
    assert any(line.startswith("3. Role swiss") for line in lines)


def test_digest_without_offers_has_no_visa_section():
    assert "VISA SPONSORSHIP" not in scraper.format_digest_message([job("plain")], 0, [])
