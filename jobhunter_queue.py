"""Market-balanced review and delivery over a caller-validated pending queue.

Callers must obtain ``eligible_candidates`` from today's get_review_candidates
with the current profile configuration. These helpers do not query a database,
extend its freshness window, lower its score threshold, or persist AI ranks.
Generic-profile callers must also verify the stored review's policy fingerprint
before admitting an old approval. A candidate missing from that validated pool
can never be introduced by a review update.
"""

import math

import job_scoring
import jobhunter_matching


_REVIEW_FIELDS = ("ai_verdict", "ai_verdict_reason", "ai_sponsorship", "ai_rank")


def _limits(per_market, cap):
    if type(per_market) is not int or type(cap) is not int or per_market < 1 or cap < 1:
        raise ValueError("per_market and cap must be positive integers")


def _pending(job):
    return job.get("notified", 0) == 0 and job.get("status", "new") == "new"


def _index(rows):
    indexed = {}
    for row in rows:
        job = dict(row)
        job_id = job.get("id")
        if type(job_id) not in (str, int) or not str(job_id).strip():
            raise ValueError("each queue candidate needs a nonempty id")
        if job_id in indexed:
            raise ValueError("duplicate queue candidate id")
        indexed[job_id] = job
    return indexed


def _score(job):
    for field in ("feedback_adjusted_score", "score"):
        value = job.get(field)
        if type(value) in (int, float) and math.isfinite(value):
            return value
    return 0


def _score_order(job):
    return (-_score(job), str(job["id"]))


def _rank_order(job):
    rank = job.get("ai_rank")
    return (rank if type(rank) is int and rank > 0 else math.inf, *_score_order(job))


def _market(job, markets):
    # Never let verdict-provided market labels override the actual location.
    return job_scoring.market_region(job.get("location"), markets=markets)


def _balanced(rows, *, key, per_market, cap):
    """One candidate per available market per round, then global spillover."""
    by_market = {}
    for job in sorted(rows, key=key):
        by_market.setdefault(job["market"], []).append(job)

    selected = []
    rounds = min(per_market, max((len(jobs) for jobs in by_market.values()), default=0))
    for offset in range(rounds):
        round_jobs = sorted((jobs[offset] for jobs in by_market.values()
                             if len(jobs) > offset), key=key)
        remaining = cap - len(selected)
        selected.extend(round_jobs[:remaining])
        if len(selected) == cap:
            return selected

    spillover = sorted((job for jobs in by_market.values()
                        for job in jobs[per_market:]), key=key)
    return selected + spillover[:cap - len(selected)]


def candidate_review_order(candidates, markets=None, per_market=3, cap=40):
    """Balance a current eligible review pool before its expensive AI cutoff.

    Each available market receives a candidate before any gets a second when
    the cap permits it. If even one each cannot fit, the best market heads win
    by review priority, current score and stable ID. Unseen jobs precede old
    approvals, then holds/uncertain decisions, within each market and spillover.
    A held row requires a fresh AI verdict; this ordering never approves it.
    Complete descriptions and all other candidate fields are preserved.
    """
    _limits(per_market, cap)
    if markets is not None:
        markets = jobhunter_matching.validate_markets(markets)
    rows = []
    for job in _index(candidates).values():
        if not _pending(job) or job.get("ai_verdict") == "reject":
            continue
        job["market"] = _market(job, markets)
        if job["market"] == "unknown":
            continue
        if markets is not None and jobhunter_matching.eligibility_reason(job, markets):
            continue
        rows.append(job)
    def priority(job):
        if not job.get("ai_verdict"):
            return (0, *_score_order(job))
        return (1 if _sendable(job, markets) else 2, *_score_order(job))

    # Review capacity is spread across every round, not only the delivery
    # floor: three early rejections must not leave a market without a chance
    # while a high-volume market consumes the remaining expensive review slots.
    return _balanced(rows, key=priority, per_market=max(per_market, cap), cap=cap)


def _sendable(job, markets):
    if not _pending(job) or job.get("ai_verdict") != "send":
        return False
    if job["market"] == "unknown":
        return False
    if markets is not None:
        return jobhunter_matching.review_sendable(job, markets)
    return job.get("ai_sponsorship") in ("offered", "implied")


def merge_reviewed_queue(eligible_candidates, newly_reviewed=(), *, markets=None):
    """Join new verdicts and still-eligible stored approvals without rank ties.

    New verdicts supersede stored verdicts, including hold/reject. Only review
    fields are copied from updates; eligibility, location and description come
    from today's validated rows. Updates absent from that pool are ignored.
    The current batch retains its AI order; old approvals follow by today's
    score, since rank 1 from separate historical batches is not comparable.
    Returned ai_rank values are unique temporary delivery ranks, never DB edits.
    """
    if markets is not None:
        markets = jobhunter_matching.validate_markets(markets)
    updates = _index(newly_reviewed)
    current, queued = [], []
    for job_id, job in _index(eligible_candidates).items():
        update = updates.get(job_id)
        if update is not None:
            for field in _REVIEW_FIELDS:
                # An incomplete new verdict must not borrow an old approval.
                job[field] = update.get(field)
        job["market"] = _market(job, markets)
        if not _sendable(job, markets):
            continue
        job["queue_original_rank"] = job.get("ai_rank")
        job["queue_origin"] = "current_review" if update is not None else "stored_review"
        (current if update is not None else queued).append(job)

    merged = sorted(current, key=_rank_order) + sorted(queued, key=_score_order)
    for rank, job in enumerate(merged, 1):
        job["ai_rank"] = rank
    return merged


def select_ranked(reviewed, *, per_market=3, cap=12, markets=None):
    """Select from merged delivery ranks with fair market floors and spillover.

    Reapply this after removing unavailable listings: newly opened slots are
    filled from the same validated queue without reranking historical batches.
    """
    _limits(per_market, cap)
    if markets is not None:
        markets = jobhunter_matching.validate_markets(markets)
    rows = []
    for job in _index(reviewed).values():
        job["market"] = _market(job, markets)
        if _sendable(job, markets):
            rows.append(job)
    selected = _balanced(rows, key=_rank_order, per_market=per_market, cap=cap)
    return sorted(selected, key=_rank_order)


def select_reviewed_queue(eligible_candidates, newly_reviewed=(), *, per_market=3,
                          cap=12, markets=None):
    """Combine current verdicts with approved backlog, then select the digest."""
    merged = merge_reviewed_queue(eligible_candidates, newly_reviewed, markets=markets)
    return select_ranked(merged, per_market=per_market, cap=cap, markets=markets)
