"""Revalidate reviewed listings and refill vacancies before recommendations leave the queue."""
from __future__ import annotations

import time

import jobhunter_availability as availability
import jobhunter_queue

MAX_CHECKS = 40
MAX_CHECK_SECONDS = 120


def revalidate(conn, job, *, checker=None):
    """Persist evidence without treating an inconclusive response as closure."""
    checker = checker or availability.check
    result = checker(job)
    availability.recordcheck(conn, job, result)
    conn.commit()
    return result


def select_available(conn, candidates, *, markets=None, per_market=3, cap=12,
                     checker=None, max_checks=MAX_CHECKS, max_seconds=MAX_CHECK_SECONDS):
    """Keep checking replacements until the selected quota is open or budget ends.

    Input must already pass today's matching/freshness checks and review-context
    validation. This function never upgrades a hold/reject or changes AI ranks.
    """
    availability.ensure_schema(conn)
    conn.commit()
    remaining = {job['id']: dict(job) for job in candidates}
    verified, outcomes, region_checks = {}, {}, {}
    deadline = time.monotonic() + max_seconds
    while (remaining and len(outcomes) < max_checks
           and time.monotonic() + availability.TOTAL_SECONDS <= deadline):
        proposed = jobhunter_queue.select_ranked(list(remaining.values()), markets=markets,
                                                per_market=per_market, cap=cap)
        # Check one entry per region first; a failing source or slow region
        # cannot consume the entire request budget before others are attempted.
        pending = [job for job in proposed if job['id'] not in outcomes]
        if not pending:
            break
        candidate = min(pending, key=lambda job: region_checks.get(job.get('market'), 0))
        region = candidate.get('market')
        region_checks[region] = region_checks.get(region, 0) + 1
        result = revalidate(conn, candidate, checker=checker)
        outcomes[candidate['id']] = result
        if result['state'] == 'open' and result.get('matched') is True:
            verified[candidate['id']] = candidate
        else:
            remaining.pop(candidate['id'], None)
    selected = jobhunter_queue.select_ranked(list(verified.values()), markets=markets,
                                            per_market=per_market, cap=cap)
    deferred = [job for job in candidates if job['id'] not in outcomes or outcomes[job['id']]['state'] == 'unknown']
    return selected, {
        'checked': len(outcomes),
        'closed': sum(value['state'] == 'closed' for value in outcomes.values()),
        'unknown': sum(value['state'] == 'unknown' for value in outcomes.values()),
        'unchecked': sum(job['id'] not in outcomes for job in candidates),
        'pending_markets': sorted({job.get('market', '') for job in deferred}),
    }
