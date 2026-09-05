# Second enriched rating round

Date: 2026-09-05

## Why

Task 9's weight refit (blocked since 2026-09-03) needs positive examples
to fit anything meaningful. The 25 ratings collected so far gave 2
positives out of 25 — every held-out split holds at most 1 good job, so
the fit "could not conclude, and the tool correctly refused to pretend
otherwise." A plain random draw of more postings would very likely repeat
that ratio (his own historical rate is roughly 5%): to get useful data
faster, this round is **enriched** — deliberately oversampled toward jobs
likely to be positive — rather than a second random sample the same size.

The scoring-engine ledger's own suggested next steps (2026-09-03) named
this as priority 4, "only after" asking him the three clarifying questions
and adding the title-seniority knockout — both done, 2026-09-04. This spec
is the step-by-step version of that one-line suggestion.

## Scope

A selection script that identifies which unrated jobs to add to the rating
sample, and the criteria for choosing them. **Not in scope:** rebuilding
or extending the rating artifact's UI/database itself (that's a separate,
capability-aware task — see "Handoff," below) — this plan produces the
candidate list only, ready to load in. Not in scope: Task 9's actual refit,
still gated on having enough positives once this round's ratings exist.

## Selection criteria

Two batches, both excluding every job_id already rated in the existing
sample:

**Batch A — score >= 75 ("excellent" band).** This is where stack_fit's
known inflation (Task 6/the 2026-09-03 spec's "blocking question") is most
likely to be hiding a bad match behind a high score — rating these
specifically tests whether the rubric's top tier is trustworthy, which the
first 25 ratings couldn't answer (most of that sample scored in the 45-75
range, not the top band).

**Batch B — resembling the two jobs he already rated good.** Both liked
jobs ("Software Backend Engineer", "Full Stack Technical Lead") are
hands-on engineering titles, not architect titles — per the structural
finding recorded in the 2026-09-03 ledger. Select unrated jobs whose
`role_fit` places them in the same family/families as those two — **the
implementer must confirm which family/families that actually is by
computing `job_scoring.role_fit` on the two real job records** (fetched
from the rating artifact, see Step 1), not by assuming "backend engineer"
(0.8) and "full stack" (0.4) match what the code actually returns:
`ROLE_FAMILIES` is checked in tuple order and "the first family whose
pattern matches wins," so a title containing both "full stack" and
"technical lead" resolves to whichever family's tuple comes first, which
may not be the one a person would guess by reading the title. Get the real
number before building the query around it — the same discipline every
other selection/measurement in this project has used.

**Total size**: aim for 30-40 new candidates across both batches (fewer
than the original 60-drawn/25-rated round — the point of "enriched" is a
smaller, more targeted sample skewed toward likely positives, not a bigger
random one). If a batch has fewer than half its target after excluding
already-rated jobs, take what exists rather than padding with borderline
matches — an honest small batch beats a diluted one.

**Deterministic, reproducible selection**: order each batch by score
descending and take the top N, rather than a random sample — so a second
run (e.g. after the AI stack ring plan changes `stack_fit`) produces a
comparable, explainable list rather than a different random draw each
time.

## Handoff: loading the batch into the rating artifact

The existing rating sample lives in a Claude.ai artifact (a published page
with its own `labels` collection, referenced in the 2026-09-03 ledger at
`https://claude.ai/code/artifact/384b1302-3443-4f91-bb06-cc8b556a271e`).
Extending that artifact with a new batch of jobs to rate is a **separate
step**, out of this plan's scope, because it requires reading the
artifact-capabilities guidance before touching a published artifact's
database or UI (per this session's own tool instructions) — this plan's
job is to produce the candidate list; loading it into the artifact is the
next action once this list exists, done in a session with that context
loaded.

## Acceptance

- The selection script (Task 1) is run against the real `data/jobs.db`
  and produces a JSON file with the candidate batch: job id, title,
  company, score, `role_fit`, `stack_fit`, and which batch (A or B) each
  came from.
- The two liked jobs' actual `role_fit` values are confirmed (fetched from
  the artifact, computed directly), not assumed from the title alone.
- The script excludes every already-rated job_id, confirmed by checking
  the output batch has zero overlap with the existing sample.
- Report the final batch size and its breakdown by batch (A vs B), plus
  each batch's size before the 30-40 cap was applied, so a future reader
  can tell whether the cap actually bound anything or whether one or both
  criteria simply didn't have that many candidates.

## Out of scope

Loading the batch into the rating artifact (see Handoff). Task 9's actual
refit. Any change to `job_scoring.py`'s scoring logic — this plan only
selects existing, already-scored jobs; it does not change how they're
scored (that's the AI stack ring plan, a separate piece of work — if both
plans are executed, run the AI stack ring plan first, since it changes
`stack_fit`, and this plan's `role_fit`/`stack_fit` reporting should
reflect whichever scoring is live at the time the batch is drawn).
