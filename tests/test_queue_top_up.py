"""Silent postings send, and a market a review round left empty can be topped up."""
import pytest

import job_scoring
import jobhunter_queue as queue

from test_queue_backfill import approved, ids, job, policy


def test_a_silent_posting_sends_instead_of_being_read_as_a_market_risk():
    # 97.7% of the corpus says nothing about sponsorship. Reading that silence
    # as doubtful in Switzerland and implied in the Gulf is the same evidence
    # scored two ways, and it kept Switzerland out of every digest.
    rows = [approved("gulf", "Dubai", ai_sponsorship="no_info"),
            approved("swiss", "Switzerland", ai_sponsorship="no_info", ai_rank=2)]
    assert ids(queue.select_reviewed_queue(rows)) == ["gulf", "swiss"]


def test_a_barrier_the_posting_states_still_blocks_delivery():
    rows = [approved("doubtful", ai_sponsorship="doubtful"),
            approved("excluded", ai_sponsorship="excluded", ai_rank=2),
            approved("silent", ai_sponsorship="no_info", ai_rank=3)]
    assert ids(queue.select_reviewed_queue(rows)) == ["silent"]


def test_select_sendable_accepts_the_same_three_reads():
    reviewed = [{"id": read, "market": "dubai", "ai_verdict": "send",
                 "ai_sponsorship": read, "ai_rank": rank}
                for rank, read in enumerate(job_scoring.SPONSORSHIP_READS, 1)]
    assert {row["id"] for row in job_scoring.select_sendable(reviewed)} == set(
        job_scoring.SENDABLE_SPONSORSHIP)


def test_a_candidate_needing_a_visa_still_needs_an_explicit_offer():
    # no_info relaxes the owner's own digest, never a scoped candidate's
    # legal eligibility: silence is not an offer of sponsorship.
    rows = [approved(location="France", ai_sponsorship="no_info")]
    assert queue.select_reviewed_queue(rows, markets=[policy("France", "sponsorship_required")]) == []
    assert ids(queue.select_reviewed_queue(rows, markets=[policy("France", "authorized")])) == ["one"]


def plan(candidates, reviewed=(), **kwargs):
    return queue.delivery_plan(candidates, reviewed, per_market=3, cap=12, **kwargs)


def test_delivery_plan_names_the_markets_a_round_would_leave_empty():
    candidates = [job("dubai-1"), job("swiss-1", "Switzerland"), job("swiss-2", "Switzerland")]
    result = plan(candidates, [{"id": "dubai-1", "ai_verdict": "send",
                                "ai_sponsorship": "no_info", "ai_rank": 1}])
    assert result["empty_markets"] == ["switzerland"]
    assert result["markets"]["dubai"]["selected"] == 1
    assert result["markets"]["switzerland"] == {"selected": 0, "eligible": 2, "reviewable": 2}


def test_delivery_plan_counts_a_held_candidate_as_still_reviewable():
    # The hold is what a second round exists to revisit, so it must be counted.
    candidates = [job("swiss-1", "Switzerland", ai_verdict="hold"),
                  job("swiss-2", "Switzerland", ai_verdict="reject")]
    assert plan(candidates)["markets"]["switzerland"] == {
        "selected": 0, "eligible": 2, "reviewable": 1}


def test_delivery_plan_does_not_count_this_round_as_topping_itself_up():
    candidates = [job("swiss-1", "Switzerland")]
    reviewed = [{"id": "swiss-1", "ai_verdict": "hold", "ai_sponsorship": "doubtful",
                 "ai_rank": None}]
    assert plan(candidates, reviewed)["markets"]["switzerland"]["reviewable"] == 0


def test_delivery_plan_reports_every_market_with_an_eligible_candidate():
    candidates = [job("dubai-1"), job("jeddah-1", "Jeddah")]
    assert sorted(plan(candidates)["markets"]) == ["dubai", "jeddah"]
    assert plan(candidates)["empty_markets"] == ["dubai", "jeddah"]


def test_top_up_order_returns_only_the_requested_markets():
    candidates = [job(f"dubai-{i}") for i in range(5)]
    candidates += [job(f"swiss-{i}", "Switzerland") for i in range(5)]
    result = queue.top_up_order(candidates, ["switzerland"], cap=12)
    assert {row["id"] for row in result} == {f"swiss-{i}" for i in range(5)}


def test_top_up_order_skips_what_the_first_round_already_judged():
    candidates = [job(f"swiss-{i}", "Switzerland") for i in range(5)]
    result = queue.top_up_order(candidates, ["switzerland"],
                                exclude_ids=["swiss-0", "swiss-1"], cap=12)
    assert {row["id"] for row in result} == {"swiss-2", "swiss-3", "swiss-4"}


def test_top_up_order_balances_several_empty_markets_and_honours_the_cap():
    candidates = [job(f"swiss-{i}", "Switzerland") for i in range(10)]
    candidates += [job(f"jeddah-{i}", "Jeddah") for i in range(10)]
    result = queue.top_up_order(candidates, ["switzerland", "jeddah"], cap=4)
    assert len(result) == 4
    assert len({row["market"] for row in result}) == 2


def test_top_up_order_rejects_an_unknown_or_empty_market_list():
    for wanted in ([], [""], ["unknown"]):
        with pytest.raises(ValueError):
            queue.top_up_order([job()], wanted)
