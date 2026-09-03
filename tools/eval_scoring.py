"""Measure a scorer against the jobs he actually judged.

AUC is the chance a job he marked interested outranks one he skipped. 0.5 is a
coin flip. The keyword scorer this replaces measured 0.565.

Imports job_scoring only, never scraper.py: the measurement must not depend
on the scraper's config or its database helpers.
"""

import itertools
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import job_scoring

BASELINE_AUC = 0.565
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"
MARKETS = ("dubai", "abu dhabi", "jeddah", "switzerland", "riyadh",
           "saudi", "united arab emirates", "sharjah")

# Rows written by the test harness, not by the scraper. Their labels are not
# his judgement, so they say nothing about ranking. This tool never reads the
# score column, so the hand-set 99 one of them carries could only skew the
# stored-score baseline, not this measurement.
FAKE = ("JobHunter Test", "Example FinTech", "Example SaaS")

# The rubric's constants (agency names, stack rings, band floors) were derived
# from the same data/jobs.db these labels live in. There is no held-out split,
# so the figure is a fit-set measurement and will read higher than the rubric
# performs on jobs it has not seen.
IN_SAMPLE_NOTE = (
    "in-sample: measured on the only labelled rows that exist, and the "
    "rubric's constants were derived from the same rows. Task 9 refits the "
    "weights against an unbiased sample and must not be re-scored on these."
)


def auc(positives, negatives):
    """Chance a positive outranks a negative, ties counting half."""
    if not positives or not negatives:
        return 0.5
    wins = sum(1 for a, b in itertools.product(positives, negatives) if a > b)
    ties = sum(1 for a, b in itertools.product(positives, negatives) if a == b)
    return (wins + 0.5 * ties) / (len(positives) * len(negatives))


def load_labels(db_path=DEFAULT_DB):
    """Return (interested, skipped) job dicts, excluding test fixtures."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(FAKE))
    sql = (
        f"SELECT * FROM jobs WHERE status = ? AND company NOT IN ({placeholders}) "
        "AND title NOT LIKE 'TEST RUN%' AND title NOT LIKE 'CTA Button%'"
    )
    interested = [dict(r) for r in conn.execute(sql, ("interested", *FAKE))]
    skipped = [dict(r) for r in conn.execute(sql, ("skipped", *FAKE))]
    conn.close()
    return interested, skipped


def _freshness_neutral_total(result):
    """The total with freshness pinned to its undated value.

    In the labelled set most interested jobs carry no date (0.7) while many
    skipped ones carry an old one (0.2), so raw AUC partly measures "has a
    date". In production every job is fresh when scored, so that part of the
    figure does not transfer. Knocked-out jobs stay at 0.
    """
    if not result["passed"]:
        return 0
    parts = dict(result["parts"], freshness=job_scoring.UNDATED_FRESHNESS)
    return round(sum(parts[name] * job_scoring.WEIGHTS[name] for name in job_scoring.WEIGHTS))


def report(db_path=DEFAULT_DB, *, now=None):
    """Score both label sets and return the comparison against the baseline.

    `auc` is the raw figure; `auc_freshness_neutral` holds freshness constant
    and is the one that predicts production. A knocked-out job scores 0, so an
    interested job that trips a knockout drags AUC down exactly as a badly
    ranked one would; `knocked_out_positives` counts those by reason.
    `interested` carries each interested job's parts so a weak dimension is
    visible rather than buried in the total.
    """
    interested, skipped = load_labels(db_path)

    def results(jobs):
        return [job_scoring.evaluate(j, allowed_locations=MARKETS, now=now) for j in jobs]

    scored_interested, scored_skipped = results(interested), results(skipped)
    positives = [r["total"] for r in scored_interested]
    negatives = [r["total"] for r in scored_skipped]
    neutral_positives = [_freshness_neutral_total(r) for r in scored_interested]
    neutral_negatives = [_freshness_neutral_total(r) for r in scored_skipped]
    above = sum(1 for r in scored_interested if job_scoring.sendable(r))
    knocked = Counter(r["reason"] for r in scored_interested if r["reason"])
    return {
        "auc": round(auc(positives, negatives), 3),
        "auc_freshness_neutral": round(auc(neutral_positives, neutral_negatives), 3),
        "baseline_auc": BASELINE_AUC,
        "in_sample": IN_SAMPLE_NOTE,
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "above_cutoff": f"{above}/{len(positives)}",
        "knocked_out_positives": dict(knocked),
        "interested": [
            {
                "title": job.get("title"),
                "company": job.get("company"),
                "total": result["total"],
                "reason": result["reason"],
                "parts": result["parts"],
            }
            for job, result in zip(interested, scored_interested)
        ],
    }


def _print(summary):
    print(f"{'auc_freshness_neutral':22} {summary['auc_freshness_neutral']}   <- predicts production")
    print(f"{'auc':22} {summary['auc']}   <- raw; freshness artifact included")
    print(f"{'baseline_auc':22} {summary['baseline_auc']}")
    print(f"{'in_sample':22} {summary['in_sample']}")
    for key in ("n_positive", "n_negative", "above_cutoff"):
        print(f"{key:22} {summary[key]}")
    knocked = summary["knocked_out_positives"]
    print(f"{'knocked_out_positives':22} {sum(knocked.values())}")
    for reason, count in sorted(knocked.items(), key=lambda kv: -kv[1]):
        print(f"{'':22} {count}  {reason}")

    names = tuple(job_scoring.WEIGHTS)
    print()
    print(f"{'total':>5}  " + "  ".join(f"{n:>9}" for n in names) + "  interested job")
    for row in sorted(summary["interested"], key=lambda r: -r["total"]):
        parts = "  ".join(f"{row['parts'][n]:>9.2f}" for n in names)
        label = f"{row['title']} — {row['company']}"
        if row["reason"]:
            label += f"  [{row['reason']}]"
        print(f"{row['total']:>5}  {parts}  {label}")


if __name__ == "__main__":
    _print(report(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB))
