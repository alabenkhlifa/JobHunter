"""Review slots go where the good jobs are; sponsors first; holds expire."""
from datetime import datetime, timedelta, timezone

import jobhunter_queue as queue

NOW = datetime(2026, 9, 12, 18, tzinfo=timezone.utc)
REGIONS = ("Dubai", "Abu Dhabi", "Jeddah", "Riyadh", "Switzerland")


def job(job_id, location="Dubai", **changes):
    row = {"id": job_id, "location": location, "title": f"Backend Engineer {job_id}",
           "company": f"Co {job_id}", "score": 80, "description": "Java backend.",
           "notified": 0, "status": "new", "ai_verdict": "", "ai_sponsorship": "",
           "ai_rank": None, "date_posted": NOW.isoformat(), "date_scraped": NOW.isoformat()}
    row.update(changes)
    return row


def ids(rows):
    return [row["id"] for row in rows]


def test_floor_per_market_then_the_rest_globally_by_score():
    candidates = [job(f"{region}-{i:02}", region, score=100 - index * 10)
                  for index, region in enumerate(REGIONS) for i in range(30)]
    selected = queue.candidate_review_order(candidates, cap=40, per_market=3)
    counts = {region: sum(row["location"] == region for row in selected) for region in REGIONS}
    assert counts == {"Dubai": 28, "Abu Dhabi": 3, "Jeddah": 3, "Riyadh": 3, "Switzerland": 3}


def test_a_promised_visa_enters_the_batch_first_even_at_a_low_score():
    candidates = [job(f"d{i}", score=99) for i in range(45)]
    candidates.append(job("visa", "Riyadh", score=36, sponsorship_signal="offered",
                          sponsorship_evidence="We sponsor the employment visa."))
    selected = queue.candidate_review_order(candidates, cap=40)
    assert ids(selected)[0] == "visa"
    assert len(selected) == 40


def test_a_hold_from_an_older_rubric_or_older_than_two_days_competes_as_unseen():
    stale_rubric = job("stale-rubric", score=95, ai_verdict="hold", ai_verdict_reason="old rule",
                       ai_rubric="r1", ai_reviewed_at=NOW.isoformat())
    stale_time = job("stale-time", score=94, ai_verdict="hold", ai_rubric="r2",
                     ai_reviewed_at=(NOW - timedelta(days=3)).isoformat())
    fresh_hold = job("fresh-hold", score=93, ai_verdict="hold", ai_rubric="r2",
                     ai_reviewed_at=(NOW - timedelta(hours=20)).isoformat())
    unseen = job("unseen", score=50)
    selected = queue.candidate_review_order([fresh_hold, stale_time, stale_rubric, unseen],
                                            cap=40, now=NOW, rubric="r2", hold_days=2)
    assert ids(selected) == ["stale-rubric", "stale-time", "unseen", "fresh-hold"]
    expired = {row["id"]: row for row in selected}
    assert expired["stale-rubric"]["ai_verdict"] == ""
    assert expired["stale-rubric"]["previous_verdict"] == "hold"
    assert expired["stale-rubric"]["ai_verdict_reason"] == "old rule"
    assert expired["fresh-hold"]["ai_verdict"] == "hold"


def test_without_a_clock_holds_keep_the_old_ordering():
    hold = job("hold", score=95, ai_verdict="hold", ai_rubric="r1",
               ai_reviewed_at=(NOW - timedelta(days=9)).isoformat())
    assert ids(queue.candidate_review_order([hold, job("unseen", score=50)])) == ["unseen", "hold"]


def test_a_posting_about_to_leave_the_window_jumps_a_slightly_better_fresh_one():
    old = job("old", score=70, date_posted=(NOW - timedelta(days=6)).isoformat())
    fresh = job("fresh", score=75)
    much_better = job("best", score=90)
    selected = queue.candidate_review_order([fresh, old, much_better], now=NOW, max_age_days=7)
    assert ids(selected) == ["best", "old", "fresh"]


def test_the_batch_keeps_one_copy_of_a_cross_posted_role():
    linkedin = job("li", title="Senior AI Cloud Architect", company="EPAM", score=60)
    foundit = job("foundit", title="Senior AI Cloud Architect", company="EPAM", score=58)
    riyadh = job("ksa", "Riyadh", title="Senior AI Cloud Architect", company="EPAM", score=55)
    assert ids(queue.candidate_review_order([foundit, linkedin, riyadh])) == ["li", "ksa"]


def test_top_up_passes_the_clock_through():
    stale = job("stale", "Jeddah", score=90, ai_verdict="hold", ai_rubric="r1",
                ai_reviewed_at=NOW.isoformat())
    ordered = queue.top_up_order([stale, job("d", score=99)], ["jeddah"],
                                 now=NOW, rubric="r2")
    assert ids(ordered) == ["stale"] and ordered[0]["ai_verdict"] == ""


def test_generic_title_roles_keep_distinct_descriptions_in_review():
    rows = [job("java", title="Architect", company="Virtusa", description="Java payments", score=80),
            job("node", title="Architect", company="Virtusa", description="Node.js identity", score=75),
            job("twin", title="Architect", company="Virtusa", description="  JAVA   payments ", score=70)]
    assert ids(queue.candidate_review_order(rows)) == ["java", "node"]


def test_long_title_reposts_still_share_a_review_slot():
    rows = [job("one", title="Java Payment Software Architect", company="Virtusa", description="One", score=80),
            job("two", title="Java Payment Software Architect", company="Virtusa", description="Two", score=70)]
    assert ids(queue.candidate_review_order(rows)) == ["one"]


def test_generic_profiles_keep_their_existing_review_identity():
    rows = [job("one", title="Architect", company="Example", description="One"),
            job("two", title="Architect", company="Example", description="One")]
    markets = [{"name": "Dubai", "locations": ["Dubai"], "work_authorization": "authorized", "relocation_required": False}]
    assert ids(queue.candidate_review_order(rows, markets=markets)) == ["one", "two"]


def test_generic_profiles_do_not_inherit_owner_sponsorship_priority():
    markets = [{"name": "Dubai", "locations": ["Dubai"], "work_authorization": "authorized", "relocation_required": False}]
    rows = [job("best", score=90, ai_verdict="send", ai_rank=1, ai_sponsorship="no_info"),
            job("visa", score=40, ai_verdict="send", ai_rank=2, ai_sponsorship="offered", sponsorship_signal="offered")]
    assert ids(queue.candidate_review_order(rows, markets=markets, cap=1)) == ["best"]
    assert ids(queue.select_ranked(rows, markets=markets, cap=1)) == ["best"]


def test_generic_profiles_keep_existing_age_and_hold_priority_with_a_clock():
    markets = [{"name": "Dubai", "locations": ["Dubai"], "work_authorization": "authorized", "relocation_required": False}]
    rows = [job("old", score=70, date_posted=(NOW - timedelta(days=6)).isoformat()),
            job("fresh", score=75),
            job("held", score=99, ai_verdict="hold", ai_rubric="old")]
    selected = queue.candidate_review_order(rows, markets=markets, now=NOW, rubric="new")
    assert ids(selected) == ["fresh", "old", "held"]
    assert selected[-1]["ai_verdict"] == "hold"


def test_generic_delivery_keeps_rank_order_after_market_allocation():
    markets = [{"name": city, "locations": [city], "work_authorization": "authorized", "relocation_required": False}
               for city in ("Dubai", "Riyadh")]
    rows = [job("first", ai_verdict="send", ai_rank=1, ai_sponsorship="no_info"),
            job("second", ai_verdict="send", ai_rank=2, ai_sponsorship="no_info"),
            job("third", "Riyadh", ai_verdict="send", ai_rank=3, ai_sponsorship="no_info")]
    assert ids(queue.select_ranked(rows, markets=markets, cap=3)) == ["first", "second", "third"]
