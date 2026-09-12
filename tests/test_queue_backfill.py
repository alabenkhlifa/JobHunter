import copy
from datetime import datetime, timedelta, timezone

import pytest

import jobhunter_queue as queue
import scraper


def job(job_id="one", location="Dubai", **changes):
    result = {
        "id": job_id, "location": location, "market": "untrusted label",
        # One company per id: the batch keeps a single copy of a cross-posted role.
        "title": "Backend Engineer", "company": f"Example {job_id}", "score": 80,
        "description": "Java Spring Boot AWS backend services.",
        "notified": 0, "status": "new", "ai_verdict": "",
        "ai_sponsorship": "", "ai_rank": None,
    }
    result.update(changes)
    return result


def approved(job_id="one", location="Dubai", **changes):
    # Silence is the normal read; a verified "offered" is rare and tested explicitly.
    result = job(job_id, location, ai_verdict="send", ai_sponsorship="no_info", ai_rank=1)
    result.update(changes)
    return result


def policy(name, authorization="authorized", relocation=False):
    return {"name": name, "locations": [name], "work_authorization": authorization,
            "relocation_required": relocation}


def ids(rows):
    return [row["id"] for row in rows]


def test_global_top40_cannot_crowd_out_eligible_thin_markets():
    candidates = [job(f"dubai-{i:02}", score=99) for i in range(60)]
    candidates += [job(f"swiss-{i:02}", "Switzerland", score=90) for i in range(40)]
    candidates += [job(f"jeddah-{i}", "Jeddah", score=50) for i in range(3)]
    selected = queue.candidate_review_order(candidates)
    assert len(selected) == 40
    assert {"jeddah-0", "jeddah-1", "jeddah-2"} <= set(ids(selected))
    assert {row["market"] for row in selected[:3]} == {"dubai", "switzerland", "jeddah"}
    assert ids(queue.candidate_review_order(list(reversed(candidates)))) == ids(selected)


def test_capacity_below_combined_quota_still_covers_each_available_market():
    candidates = [job(f"{region}-{i}", region, score=100 - region_index * 10)
                  for region_index, region in enumerate(("Dubai", "Abu Dhabi", "Jeddah", "Riyadh", "Switzerland"))
                  for i in range(3)]
    selected = queue.candidate_review_order(candidates, cap=6)
    assert len(selected) == 6
    assert len({row["market"] for row in selected}) == 5
    assert sum(row["market"] == "dubai" for row in selected) == 2


def test_capacity_below_market_count_uses_best_heads_deterministically():
    candidates = [job("d", "Dubai", score=90), job("d2", "Dubai", score=99),
                  job("j", "Jeddah", score=60), job("s", "Switzerland", score=80)]
    assert ids(queue.candidate_review_order(candidates, cap=2)) == ["d2", "s"]


def test_unseen_rows_precede_high_score_old_holds_and_uncertain_approvals():
    candidates = [approved(f"held-{i}", "Switzerland", ai_verdict="hold", score=100)
                  for i in range(45)]
    candidates += [approved("uncertain", "Switzerland", ai_sponsorship="doubtful", score=100),
                   approved("old-approved", "Switzerland", score=95),
                   job("unseen", "Switzerland", score=46)]
    ordered = queue.candidate_review_order(candidates, cap=40)
    assert ids(ordered)[:2] == ["unseen", "old-approved"]
    assert queue.select_reviewed_queue(candidates) == [queue.merge_reviewed_queue(candidates)[0]]
    assert ids(queue.select_reviewed_queue(candidates)) == ["old-approved"]


def test_feedback_score_orders_unseen_candidates_without_overriding_market_coverage():
    candidates = [job("one", score=95, feedback_adjusted_score=50),
                  job("two", score=60, feedback_adjusted_score=80),
                  job("jeddah", "Jeddah", score=46)]
    assert ids(queue.candidate_review_order(candidates, cap=2)) == ["two", "jeddah"]


def test_unused_review_capacity_flows_to_the_only_available_market():
    candidates = [job(str(i), score=100 - i) for i in range(8)]
    assert ids(queue.candidate_review_order(candidates, cap=6)) == [str(i) for i in range(6)]


def test_complete_description_survives_ordering():
    description = "Full requirements. " * 1000 + "No visa sponsorship."
    selected = queue.candidate_review_order([job(description=description)])
    assert selected[0]["description"] == description


def test_prior_approvals_fill_thin_markets_and_ranks_do_not_collide_between_batches():
    current = [job(f"new-{i}", score=99) for i in range(5)]
    stored = [approved("jeddah", "Jeddah", score=60, ai_rank=1),
              approved("swiss", "Switzerland", score=70, ai_rank=1),
              approved("riyadh", "Riyadh", score=50, ai_rank=1)]
    new_verdicts = [approved(f"new-{i}", ai_rank=5 - i) for i in range(5)]
    merged = queue.merge_reviewed_queue(current + stored, new_verdicts)
    assert ids(merged) == ["new-4", "new-3", "new-2", "new-1", "new-0", "swiss", "jeddah", "riyadh"]
    assert [row["ai_rank"] for row in merged] == list(range(1, 9))
    assert [row["queue_original_rank"] for row in merged[-3:]] == [1, 1, 1]
    selected = queue.select_ranked(merged, cap=6)
    assert {"swiss", "jeddah", "riyadh"} <= set(ids(selected))
    assert len(selected) == 6
    assert len({row["market"] for row in selected}) == 4


def test_queued_approval_is_still_available_when_there_are_no_new_verdicts():
    assert ids(queue.select_reviewed_queue([approved("queued", "Jeddah")])) == ["queued"]


def test_unavailable_listing_removal_refills_market_from_next_ranked_candidate():
    merged = queue.merge_reviewed_queue([
        approved("closed", "Jeddah", score=90), approved("open", "Jeddah", score=80),
        approved("dubai", score=99),
    ])
    assert set(ids(queue.select_ranked(merged, cap=2))) == {"closed", "dubai"}
    still_available = [row for row in merged if row["id"] != "closed"]
    assert set(ids(queue.select_ranked(still_available, cap=2))) == {"open", "dubai"}


@pytest.mark.parametrize("changes", [
    {"ai_verdict": "hold"}, {"ai_verdict": "reject"}, {"ai_verdict": ""},
    {"ai_sponsorship": "doubtful"}, {"ai_sponsorship": "excluded"},
    {"notified": 1}, {"status": "rejected"}, {"status": "applied"},
])
def test_ineligible_queue_states_never_backfill(changes):
    assert queue.select_reviewed_queue([approved(**changes)]) == []


def test_new_hold_or_reject_supersedes_a_prior_send_without_resurrection():
    current = [approved("hold"), approved("reject"), approved("incomplete")]
    updates = [{"id": "hold", "ai_verdict": "hold"},
               {"id": "reject", "ai_verdict": "reject"}, {"id": "incomplete"}]
    assert queue.merge_reviewed_queue(current, updates) == []


def test_new_verdict_cannot_inject_missing_stale_job_or_modify_eligibility_fields():
    candidate = job("valid", "Jeddah", score=50)
    updates = [approved("outside-current-pool"),
               approved("valid", "Dubai", score=100, description="Invented eligibility", notified=0)]
    result = queue.merge_reviewed_queue([candidate], updates)
    assert ids(result) == ["valid"]
    assert result[0]["market"] == "jeddah"
    assert result[0]["score"] == 50
    assert result[0]["description"] == candidate["description"]
    assert queue.merge_reviewed_queue([dict(candidate, notified=1)], updates) == []


@pytest.mark.parametrize("authorization, sponsorship, expected", [
    ("authorized", "excluded", True), ("authorized", "doubtful", True),
    ("sponsorship_required", "offered", True), ("sponsorship_required", "implied", False),
    ("unknown", "offered", False),
])
def test_backfill_rechecks_current_candidate_work_authorization(authorization, sponsorship, expected):
    rows = [approved(location="France", ai_sponsorship=sponsorship)]
    result = queue.select_reviewed_queue(rows, markets=[policy("France", authorization)])
    assert bool(result) is expected


def test_current_description_refusal_and_relocation_remain_hard_gates():
    rows = [approved(location="France", description="No visa sponsorship.")]
    assert queue.select_reviewed_queue(rows, markets=[policy("France", "sponsorship_required")]) == []
    rows = [approved(location="France", description="Local candidates only. No relocation.")]
    assert queue.select_reviewed_queue(rows, markets=[policy("France", relocation=True)]) == []


def test_configurable_markets_do_not_trust_stored_or_injected_market_labels():
    rows = [approved("fr", "France", market="dubai"), approved("de", "Germany", market="france"),
            approved("unknown", "Elsewhere", market="france")]
    policies = [policy("France"), policy("Germany", "unknown")]
    result = queue.select_reviewed_queue(rows, markets=policies)
    assert ids(result) == ["fr"]
    assert result[0]["market"] == "france"


def test_helpers_do_not_mutate_input_rows_or_persist_temporary_ranks():
    rows = [approved("a", ai_rank=19), approved("b", "Jeddah", ai_rank=19)]
    original = copy.deepcopy(rows)
    queue.candidate_review_order(rows)
    queue.select_reviewed_queue(rows, rows)
    assert rows == original


@pytest.mark.parametrize("function", [queue.candidate_review_order, queue.select_ranked, queue.select_reviewed_queue])
@pytest.mark.parametrize("limits", [{"cap": 0}, {"cap": True}, {"per_market": -1}, {"per_market": 1.5}])
def test_invalid_capacity_is_rejected(function, limits):
    with pytest.raises(ValueError, match="positive integers"):
        function([], **limits)


def test_duplicate_ids_fail_instead_of_repeating_a_listing():
    with pytest.raises(ValueError, match="duplicate"):
        queue.merge_reviewed_queue([approved(), approved()])


def test_backfill_uses_today_validated_pool_without_extending_seven_day_window(tmp_path, monkeypatch):
    settings = copy.deepcopy(scraper.CONFIG)
    settings.update(db_path=str(tmp_path / "jobs.db"), max_job_age_days=7, score_threshold=45)
    monkeypatch.setattr(scraper, "CONFIG", settings)
    now = datetime.now(timezone.utc)
    postings = [approved("within-seven-days", "Jeddah"),
                approved("eight-days-old", "Jeddah"), approved("low-score", score=44),
                approved("no-sponsorship", description="No visa sponsorship."),
                approved("wrong-role", title="Junior Developer"), approved("notified", notified=1),
                approved("held", ai_verdict="hold"), approved("rejected", status="rejected")]
    conn = scraper.init_db()
    try:
        for posting in postings:
            posting.update(source="LinkedIn", url="https://example.com/job", tech_required="java, spring boot, aws",
                           tech_nice_to_have="", min_experience=5, salary="", work_model="",
                           score_breakdown="fixture", date_posted=(now - timedelta(days=8 if posting["id"] == "eight-days-old" else 7)).isoformat())
            scraper.save_job(conn, posting)
            conn.execute("UPDATE jobs SET ai_verdict=?,ai_sponsorship=?,ai_rank=?,notified=?,status=? WHERE id=?",
                         (posting["ai_verdict"], posting["ai_sponsorship"], posting["ai_rank"],
                          posting["notified"], posting["status"], posting["id"]))
        conn.commit()
        eligible = scraper.get_review_candidates(conn, now=now)
        assert ids(queue.select_reviewed_queue(eligible)) == ["within-seven-days"]
        assert queue.select_reviewed_queue(scraper.get_review_candidates(conn, now=now + timedelta(days=1))) == []
        stored = conn.execute("SELECT notified,ai_rank FROM jobs WHERE id='within-seven-days'").fetchone()
        assert tuple(stored) == (0, 1)
    finally:
        conn.close()
