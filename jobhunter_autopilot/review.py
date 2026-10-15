"""Cron A: hand the reviewer the autopilot's unjudged candidates and record its verdicts.

The envelope is the nightly collector's shape, so the same job-hunter review
rules apply; only freshness is lifted (``max_job_age_days: null``) because
the autopilot also applies to older, still-open offers. Verdicts are stored
with the same UPDATE as ``scraper.record_review`` but validated against the
autopilot's own eligible pool, which ``record_review`` would re-derive with
the 7-day window and drop.
"""

from __future__ import annotations

from datetime import datetime, timezone

import job_scoring
import scraper

from . import policy as policy_module
from . import selection, store

# Copied from the Hermes collector (jobhunter_collect_candidates.FIELDS).
FIELDS = (
    "id, title, company, location, url, source, score, date_posted, date_scraped, "
    "description, tech_required, tech_nice_to_have, min_experience, salary, "
    "work_model, score_breakdown, status, recruiter_name, recruiter_company, "
    "company_website, credibility_notes, ai_verdict, ai_verdict_reason, "
    "ai_sponsorship, ai_rank, ai_reviewed_at, ai_rubric, sponsorship_signal, sponsorship_evidence, "
    "apply_url, language_requirement"
)
FIELD_NAMES = tuple(field.strip() for field in FIELDS.split(","))
FEEDBACK_EXAMPLES = 30


def _todays_settled(conn, run_id):
    return {row["job_id"] for row in store.log_rows(conn, run_id)}


def review_pool(conn, policy, *, now=None, run_id=None):
    """(pool, needs_review) for today: every candidate that passes all filters but the verdict gate."""
    now = now or datetime.now(timezone.utc)
    run_id = run_id or store.run_id_for(now, policy["timezone"])
    result = selection.select(conn, policy, now=now, exclude_ids=_todays_settled(conn, run_id))
    return result["review_pool"], result["needs_review"]


def _candidate(job, need):
    item = {field: job.get(field) for field in FIELD_NAMES}
    item["description"] = item.get("description") or ""
    item["previous_verdict"] = None
    if need in ("hold_expired", "stale_rubric"):
        # Like jobhunter_queue._expire_hold: the old verdict rides along, unjudged.
        item["previous_verdict"] = job.get("ai_verdict") or None
        item["ai_verdict"] = ""
    item["effective_score"] = job["effective_score"]
    return item


def review_batch(conn, policy, now=None, *, limit=None, run_id=None):
    """The collector-shaped envelope plus the ``autopilot`` block. Writes nothing."""
    now = now or datetime.now(timezone.utc)
    run_id = run_id or store.run_id_for(now, policy["timezone"])
    active = policy_module.is_active(policy, now)
    limit = policy["review_batch_size"] if limit is None else max(0, int(limit))
    pool, needs = review_pool(conn, policy, now=now, run_id=run_id) if active else ([], {})
    batch = pool[:limit]
    items = [_candidate(job, needs[job["id"]]) for job in batch]
    examples = scraper.get_feedback_examples(conn, limit=FEEDBACK_EXAMPLES, initialize=False)
    return {
        "count": len(items),
        "review_constraints": {"max_experience": scraper.CONFIG["max_experience"], "max_job_age_days": None},
        "review_preferences": scraper.CONFIG.get("review_preferences", {}),
        "feedback_examples": examples,
        "candidates": items,
        "autopilot": {
            "run_id": run_id,
            "enabled": bool(policy.get("enabled")),
            "paused": policy_module.is_paused(policy, now),
            "policy_summary": policy_module.summary_text(policy, now),
            "needs_review": {job["id"]: needs[job["id"]] for job in batch},
            "pending_beyond_batch": max(0, len(pool) - len(batch)),
        },
    }


def record_review(conn, verdicts, eligible_candidates, report=None, *, now=None, rubric=None):
    """Validate with ``scraper.validated_verdicts`` and write like ``scraper.record_review``.

    ``eligible_candidates`` are the job dicts the autopilot itself considers
    reviewable now. Returns ``{recorded, send, hold, reject, review_notes}``.
    """
    report = [] if report is None else report
    rubric = scraper.CONFIG.get("review_rubric", "") if rubric is None else rubric
    eligible = {job["id"]: job.get("location") for job in eligible_candidates}
    descriptions = {job["id"]: job.get("description") or "" for job in eligible_candidates}
    generic = scraper.CONFIG.get("matching", {}).get("preset") == "generic"
    verified_evidence = ({job["id"]: job.get("sponsorship_evidence") or "" for job in eligible_candidates
                          if job.get("sponsorship_signal") == "offered"} if generic else None)
    scraper.ensure_review_columns(conn)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    counts = {"send": 0, "hold": 0, "reject": 0}
    accepted = scraper.validated_verdicts(
        verdicts, eligible, report=report, descriptions=descriptions, verified_evidence=verified_evidence,
        preserve_doubtful=any(m.get("accept_unconfirmed_sponsorship") for m in scraper.CONFIG.get("markets", [])),
    )
    for entry in accepted:
        status_update = ", status = 'rejected'" if entry["ai_verdict"] == "reject" else ""
        conn.execute(
            f"UPDATE jobs SET ai_verdict = ?, ai_verdict_reason = ?, "
            f"ai_sponsorship = ?, ai_rank = ?, ai_reviewed_at = ?, ai_rubric = ?{status_update} WHERE id = ?",
            (entry["ai_verdict"], entry["ai_verdict_reason"], entry["ai_sponsorship"],
             entry["ai_rank"], stamp, rubric, entry["id"]),
        )
        if entry.get("evidence"):
            row = conn.execute("SELECT sponsorship_signal FROM jobs WHERE id = ?", (entry["id"],)).fetchone()
            if (row is not None and row[0] != "offered"
                    and job_scoring.sponsorship_signal(entry["evidence"])[0] == "offered"):
                conn.execute("UPDATE jobs SET sponsorship_signal = 'offered', sponsorship_evidence = ? WHERE id = ?",
                             (entry["evidence"], entry["id"]))
                report.append({"job_id": entry["id"],
                               "note": "reviewer quote the pre-read missed; add its wording to job_scoring"})
        counts[entry["ai_verdict"]] += 1
    conn.commit()
    return {"recorded": len(accepted), **counts, "review_notes": report}
