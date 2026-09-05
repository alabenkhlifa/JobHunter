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

The pool the batches draw from excludes test-harness rows, jobs that already
carry a verdict from the interested/skipped label set (a second, disagreeing
label source), dead postings, and duplicate copies of the same posting
cross-listed on two boards.
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))
import eval_scoring
import job_scoring

# Statuses that already carry a verdict from a different labelling mechanism
# than the rating artifact this batch feeds. 'interested' and 'skipped' are
# exactly what eval_scoring.load_labels reads as its own labelled set, so
# rating one of those again would put two label sources on the same job, free
# to disagree. 'unavailable' is a posting confirmed dead: asking him to rate a
# job that no longer exists spends a rating slot on nothing.
#
# 'archived' and 'notified' are deliberately NOT here. Neither is a judgement
# of the job -- they record what the pipeline did with it -- so those rows are
# still unrated and still eligible.
ALREADY_JUDGED_STATUSES = ("interested", "skipped", "unavailable")


def dedupe_by_posting(rows):
    """One row per real posting, keeping the highest-scoring copy.

    The same posting is cross-posted to LinkedIn and Foundit under two ids, so
    a straight id-level selection asks him to rate the same job twice and, when
    the ratings come back, hands the weight refit that job's label twice --
    double-weighted against every posting that appeared on one board only.
    job_scoring.duplicate_key is the scraper's own identity for a posting,
    called rather than restated. Scanning score-descending and keeping the
    first hit per key keeps the copy that would have ranked, so dedup never
    changes what tops a batch.
    """
    best = {}
    for row in sorted(rows, key=lambda r: r["score"], reverse=True):
        best.setdefault(job_scoring.duplicate_key(row), row)
    return list(best.values())


def select_candidates(rows, already_rated_ids, liked_role_fits, *,
                      excellent_threshold=75, sendable_threshold=45,
                      # Per-batch cap. Each batch is its own independent
                      # selection (a job can be in both), so this bounds each
                      # batch at target_size and the union at 2 x target_size --
                      # for an arbitrary corpus with disjoint batches the union
                      # really is 2 x target_size. The 30-40 unique total this
                      # script lands on is an empirical property of THIS corpus:
                      # 18 each plus the overlap between A and B plus the
                      # posting-level dedup in main() happen to land there. It
                      # is not a guarantee the algorithm provides, and the plan's
                      # "take what exists, don't pad" applies if it comes in low.
                      target_size=18):
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
    conn = sqlite3.connect(REPO / "data" / "jobs.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM jobs").fetchall()
    conn.close()

    # Score live rather than read the stored `score` column. The column holds
    # whatever the rubric said on the day the row was written, and the rubric
    # has changed since (the AI stack ring, the tech backfill); ranking on it
    # would rank by scrape date as much as by fit. Same call shape as
    # scraper.score_job -- the scraper's markets, the default max_experience,
    # and no `now=` override. That is what fit_weights and eval_scoring.report
    # do for the same reason: the rating and refit data must reflect the
    # scorer that ships today. The one departure is freshness, which is
    # neutralised for ranking -- eval_scoring._freshness_neutral_total, called
    # rather than copied, so ranking here cannot drift from the measurement.
    scored = []
    for row in rows:
        job = dict(row)
        # eval_scoring's exclusion, called rather than restated. Harness rows
        # are not postings anyone published, and one carries a hand-set score
        # high enough to top batch A on its own.
        if eval_scoring.is_fixture(job):
            continue
        if (job.get("status") or "") in ALREADY_JUDGED_STATUSES:
            continue
        # Blanked the way eval_scoring and fit_weights blank them: the scraper
        # fills recruiter_company and credibility_notes after score_job runs,
        # so a stored row carries fields production never sees while scoring.
        job = eval_scoring.as_scored_live(job)
        result = job_scoring.evaluate(
            job, allowed_locations=job_scoring.DEFAULT_MARKETS
        )
        scored.append({
            "id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "score": eval_scoring._freshness_neutral_total(result),
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

    result = select_candidates(dedupe_by_posting(scored), already_rated_ids,
                               liked_role_fits)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
