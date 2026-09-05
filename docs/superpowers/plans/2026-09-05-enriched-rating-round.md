# Enriched Rating Round Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a batch of 30-40 unrated jobs, deliberately skewed toward
likely positives (score >= 75, or resembling the two jobs he already rated
good), ready to load into the rating artifact — the step that actually
unblocks Task 9's weight refit.

**Architecture:** One selection function in a new one-off tool script,
tested with an in-memory sqlite fixture (matching this session's
established pattern), then run once against the real `data/jobs.db` to
produce the actual batch as JSON.

**Tech Stack:** Python 3.13, stdlib only, sqlite3. pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-05-enriched-rating-round-design.md`

## Global Constraints

- Two batches, both excluding already-rated job ids: **A** = score >= 75.
  **B** = `role_fit` matching whichever family/families the two liked jobs
  ("Software Backend Engineer", "Full Stack Technical Lead") actually
  belong to — confirmed by computing `job_scoring.role_fit` on those two
  real records (fetched from the artifact), not assumed from the titles —
  plus score >= 45 (the sendable threshold).
- Selection within each batch is deterministic: order by score descending,
  take the top N. No random sampling.
- Target total size 30-40. If a batch has fewer candidates than half its
  share of that range after excluding already-rated ids, take what exists
  — do not pad with weaker matches to hit the number.
- Every existing test must still pass: `.venv/bin/python -m pytest -q tests`
  reports 384 passed as of 2026-09-05 (baseline after the digest delivery
  plan; may be 388 if the AI stack ring plan ran first — check `git log`
  for `job_scoring.py`'s `AI_STACK` before assuming which baseline applies).

---

### Task 1: Selection script

**Files:**
- Create: `tools/select_rating_candidates.py`
- Test: `tests/test_select_rating_candidates.py`

**Interfaces:**
- Consumes: `job_scoring.role_fit`, `job_scoring.stack_fit` (already exist).
- Produces: `select_candidates(rows, already_rated_ids, liked_role_fits, *,
  excellent_threshold=75, sendable_threshold=45, target_size=35) ->
  dict` with keys `"batch_a"`, `"batch_b"` (each a list of job dicts,
  score-descending), plus a CLI entry point that reads the real DB and the
  already-rated id list, and writes the result as JSON.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_select_rating_candidates.py
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


def test_target_size_caps_each_batch_but_does_not_pad():
    rows = [job(id=f"hi{i}", score=100 - i) for i in range(50)]
    result = src.select_candidates(rows, already_rated_ids=set(), liked_role_fits=set(),
                                    target_size=10)
    # target_size=10 total budget; batch_a alone must not silently take all 50.
    assert len(result["batch_a"]) <= 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_select_rating_candidates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'select_rating_candidates'`

- [ ] **Step 3: Write the implementation**

```python
# tools/select_rating_candidates.py
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
    rows = conn.execute(
        "SELECT id, title, company, location, score, tech_required, "
        "tech_nice_to_have, min_experience FROM jobs"
    ).fetchall()
    scored = []
    for row in rows:
        job = dict(row)
        job["role_fit"] = job_scoring.role_fit(job)
        job["stack_fit"] = job_scoring.stack_fit(job)
        scored.append(job)

    # already_rated_ids and liked_role_fits must be supplied by whoever runs
    # this -- see the brief's Step 4/5 for how to get the real values before
    # running this for real. Placeholder empty/example values below will
    # select nothing useful if run as-is.
    already_rated_ids = set()  # TODO before running: load from the artifact export
    liked_role_fits = set()  # TODO before running: computed from the two real liked jobs

    result = select_candidates(scored, already_rated_ids, liked_role_fits)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_select_rating_candidates.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, no regressions.

- [ ] **Step 6: Get the two liked jobs' real role_fit and the already-rated id list**

This step cannot be pre-verified the way the rest of this plan's numbers
were, because the data lives in a separate Claude.ai artifact, not
`data/jobs.db`. Before running the script for real:

1. Read the rating artifact (`https://claude.ai/code/artifact/384b1302-3443-4f91-bb06-cc8b556a271e`
   per the 2026-09-03 ledger) via the Artifact tool's `read_db` action on
   its `labels` collection, to get every already-rated `job_id` and each
   rating's verdict.
2. Find the two job_ids rated "good"/"interested" whose titles are
   "Software Backend Engineer" and "Full Stack Technical Lead" (per the
   2026-09-03 ledger's own description of them).
3. Compute `job_scoring.role_fit(...)` on each of those two real job
   records (fetched from `data/jobs.db` by id, or from the artifact export
   if the full record lives there) and record the actual values — do not
   assume 0.8/0.4 from the titles, per the spec's own warning about
   `ROLE_FAMILIES`'s ordered-match behavior.
4. Update `main()`'s two placeholder lines with the real sets before running.

- [ ] **Step 7: Run the script against the real database**

```bash
.venv/bin/python tools/select_rating_candidates.py > /tmp/rating_round_2_candidates.json
```

Report: how many candidates in batch A and batch B before any cap, how
many after (if the cap bound anything), and the total unique job count
across both batches (accounting for overlap — see the "can appear in
both" test).

- [ ] **Step 8: Commit**

```bash
git add tools/select_rating_candidates.py tests/test_select_rating_candidates.py
git commit -m "Add select_rating_candidates: the enriched round's selection logic

Two independent batches -- score >= 75 (tests whether the rubric's top
tier is trustworthy) and role_fit matching the two jobs he already
rated good (deliberately oversampling toward likely positives, since a
plain random draw would very likely repeat the first round's 2-of-25
ratio). A job can qualify for both; these are not a partition."
```

---

## What this plan does not cover

Loading the produced candidate list into the rating artifact — a separate
step requiring the artifact-capabilities skill's guidance before touching
a published artifact's database, done once this script's JSON output
exists. Task 9's actual refit, once enough ratings exist from this round.
