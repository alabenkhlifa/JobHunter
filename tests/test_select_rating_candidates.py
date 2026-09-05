import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import select_rating_candidates as src


def job(**over):
    base = {
        "id": "j1", "title": "Backend Engineer", "company": "Acme",
        "score": 80, "role_fit": 0.8, "stack_fit": 0.5,
    }
    base.update(over)
    return base


def test_batch_a_is_score_75_and_above_excluding_already_rated():
    rows = [
        job(id="hi1", score=90),
        job(id="hi2", score=76),
        job(id="already", score=95),
        job(id="lo1", score=60),
    ]
    result = src.select_candidates(rows, already_rated_ids={"already"}, liked_role_fits={0.8})
    ids = {j["id"] for j in result["batch_a"]}
    assert ids == {"hi1", "hi2"}


def test_batch_a_is_score_descending():
    rows = [job(id="a", score=76), job(id="b", score=95), job(id="c", score=80)]
    result = src.select_candidates(rows, already_rated_ids=set(), liked_role_fits={0.8})
    assert [j["id"] for j in result["batch_a"]] == ["b", "c", "a"]


def test_batch_b_matches_liked_role_fits_and_clears_sendable_threshold():
    rows = [
        job(id="match1", role_fit=0.8, score=50),
        job(id="match_but_low", role_fit=0.8, score=30),  # below sendable threshold
        job(id="no_match", role_fit=0.3, score=80),
        job(id="already", role_fit=0.8, score=60),
    ]
    result = src.select_candidates(rows, already_rated_ids={"already"}, liked_role_fits={0.8})
    ids = {j["id"] for j in result["batch_b"]}
    assert ids == {"match1"}


def test_batch_b_matches_any_of_multiple_liked_role_fits():
    rows = [
        job(id="fam_a", role_fit=0.8, score=50),
        job(id="fam_b", role_fit=0.4, score=50),
        job(id="neither", role_fit=1.0, score=50),
    ]
    result = src.select_candidates(rows, already_rated_ids=set(), liked_role_fits={0.8, 0.4})
    ids = {j["id"] for j in result["batch_b"]}
    assert ids == {"fam_a", "fam_b"}


def test_a_job_can_appear_in_both_batches_if_it_qualifies_for_both():
    # score 90 AND role_fit matches liked family -- both batches are
    # independent selections, not mutually exclusive partitions.
    rows = [job(id="both", score=90, role_fit=0.8)]
    result = src.select_candidates(rows, already_rated_ids=set(), liked_role_fits={0.8})
    assert result["batch_a"][0]["id"] == "both"
    assert result["batch_b"][0]["id"] == "both"


def test_target_size_caps_the_unique_total_across_both_batches():
    # Both batches are really populated -- the top 25 clear batch A's 75 gate,
    # and every row carries the liked role_fit at or above the sendable
    # threshold, so batch B draws from all 50 -- and the assertion is on the
    # UNION, because target_size budgets the unique set the rater sees, not
    # each batch on its own. The two batches rank the same rows by the same
    # score, so the cap holds across both.
    rows = [job(id=f"hi{i}", score=100 - i, role_fit=0.8) for i in range(50)]
    result = src.select_candidates(rows, already_rated_ids=set(), liked_role_fits={0.8},
                                    target_size=10)
    assert result["batch_a"] and result["batch_b"]
    unique = {j["id"] for j in result["batch_a"]} | {j["id"] for j in result["batch_b"]}
    assert len(unique) <= 10
