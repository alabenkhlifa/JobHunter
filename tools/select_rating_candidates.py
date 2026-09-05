"""Select unrated jobs for the second, enriched rating round.

Two batches: A (score >= 75, tests whether the rubric's top tier is
trustworthy -- the first 25 ratings mostly scored 45-75, not the top
band) and B (role_fit matching the family/families of the two jobs he
already rated good -- deliberately oversampling toward likely positives,
since a plain random draw would very likely repeat the first round's
2-of-25 ratio).

A job can appear in both batches if it qualifies for both -- these are
independent selections, not a partition. Deterministic: each batch is
ordered by score descending, no random sampling, so a second run (e.g.
after a scoring change) produces a comparable list.
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def select_candidates(rows, already_rated_ids, liked_role_fits, *,
                      excellent_threshold=75, sendable_threshold=45,
                      target_size=35):
    unrated = [r for r in rows if r["id"] not in already_rated_ids]

    batch_a = sorted(
        (r for r in unrated if r["score"] >= excellent_threshold),
        key=lambda r: r["score"], reverse=True,
    )[:target_size]

    batch_b = sorted(
        (r for r in unrated
         if r["role_fit"] in liked_role_fits and r["score"] >= sendable_threshold),
        key=lambda r: r["score"], reverse=True,
    )[:target_size]

    return {"batch_a": batch_a, "batch_b": batch_b}


def main():
    sys.path.insert(0, str(REPO))
    import job_scoring

    conn = sqlite3.connect(REPO / "data" / "jobs.db")
    conn.row_factory = sqlite3.Row
    # `test-%` ids are rows the Telegram/CTA test harness wrote, not postings.
    # They must never reach the rating artifact -- one of them carries a
    # hand-set score high enough to top batch A on its own.
    rows = conn.execute(
        "SELECT * FROM jobs WHERE id NOT LIKE 'test-%'"
    ).fetchall()

    # Score live rather than read the stored `score` column. The column holds
    # whatever the rubric said on the day the row was written, and the rubric
    # has changed since (the AI stack ring, the tech backfill); ranking on it
    # would rank by scrape date as much as by fit. Same call shape as
    # scraper.score_job -- the scraper's markets, the default max_experience,
    # and no `now=` override, so freshness is live. That is what fit_weights
    # and eval_scoring.report do for the same reason: the rating and refit
    # data must reflect the scorer that ships today.
    scored = []
    for row in rows:
        job = dict(row)
        result = job_scoring.evaluate(
            job, allowed_locations=job_scoring.DEFAULT_MARKETS
        )
        scored.append({
            "id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "score": result["total"],
            "tech_required": job["tech_required"],
            "tech_nice_to_have": job["tech_nice_to_have"],
            "min_experience": job["min_experience"],
            "role_fit": job_scoring.role_fit(job),
            "stack_fit": job_scoring.stack_fit(job),
        })

    # The 25 postings rated in the first round, and the role_fit values of the
    # two he rated good ("Software Backend Engineer" -> 0.8, "Full Stack
    # Technical Lead" -> 0.4, both recomputed against the current records
    # rather than assumed from the titles).
    already_rated_ids = {
        "foundit-46517522", "foundit-46518281", "foundit-46881004",
        "li-4369541501", "li-4369767026", "li-4375731378", "li-4377406502",
        "li-4377655415", "li-4378311960", "li-4379508989", "li-4380096476",
        "li-4384804316", "li-4389948892", "li-4393800396", "li-4399376783",
        "li-4400447047", "li-4402140553", "li-4403885116", "li-4409936867",
        "li-4415927699", "li-4424346527", "li-4427346309", "li-4432321399",
        "li-4433941657", "li-4435998058",
    }
    liked_role_fits = {0.8, 0.4}

    result = select_candidates(scored, already_rated_ids, liked_role_fits)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
