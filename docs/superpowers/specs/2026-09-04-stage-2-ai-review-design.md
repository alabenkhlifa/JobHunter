# Stage 2: the AI review

Date: 2026-09-04

## Why

`docs/superpowers/specs/2026-09-03-job-scoring-design.md` specced a two-stage
cascade — knockouts and a weighted score in stage 1, an AI review in stage 2
— and left stage 2 unbuilt, deferred until stage 1 cleared its AUC gate
(Task 6, 2026-09-03: 0.762/0.744, well above the 0.565 baseline).

Investigating before designing anything found stage 2 already exists, in a
crude form, and is live: a Hermes cron job (`job-hunter-daily`, on the Pi,
`0 18 * * *`) already ran once (2026-09-03T18:47, `last_status: "ok"`) and
fires nightly. `~/.hermes/scripts/jobhunter_collect_candidates.py` runs the
scraper in `--collect-only` mode, then prints unnotified `status='new'`
candidates scoring `>= score_threshold` as JSON — title, company, location,
tech, a 1200-char truncated description, `score_breakdown` (the five
job_scoring dimensions), and `feedback_learning_notes` (`apply_feedback_learning`,
a pre-existing lightweight ranking signal from Interested/Skip history). That
JSON is exactly the spec's stage-2 input contract already. The gaps are all on
the judgment and selection side:

- The Hermes prompt is loose natural language ("reject junior/frontend/
  QA/ML/DevOps roles, outside-market roles, no-relocation roles... keep at
  most the 5 best"), not the spec's strict per-job JSON contract.
- Nothing is written back to the database. The agent's reasoning is entirely
  transient — no verdict, reason, or sponsorship read survives past the
  Telegram send, so nothing can be audited, and a job it holds today gets no
  memory tomorrow beyond the raw score.
- The 5-best cap is flat, not the spec's top-3-per-market-with-spillover-to-12.
- `MAX_CANDIDATES = 25` in the collector; the spec's stage-2 input cap is 40.
- Sends still go out one Telegram message per job (`jobhunter_notify.py`,
  `notify_new_jobs`), not the spec's single nightly digest.

## Scope for this round

The review contract, its persistence, and the mechanical selection algorithm.
**Not** the digest message format or its drill-down callbacks (`show:<job_id>`,
`more:<date>`) — that is a separate, UI-shaped design pass. This round keeps
today's per-job Telegram card as the delivery mechanism, gated by the new
selection instead of the agent's own ad hoc choice. Also not in scope: fixing
`feedback_adjusted_score`/`apply_feedback_learning` (pre-existing, untouched),
and not the stack_fit blocking question from the 2026-09-03 spec (Task 9 is
still blocked on the user's 60 ratings; stage 2's own judgment is one of the
two mitigations for it, alongside — not instead of — the eventual refit).

## Contract

### Input

Unchanged — already built by `jobhunter_collect_candidates.py`. One fix:
`MAX_CANDIDATES` 25 → 40, matching the spec's stage-2 input cap.

### Output: strict JSON per job

The agent returns a JSON array on stdin to the new `jobhunter_review.py`
script, one entry per job it reviewed:

```json
[
  {
    "job_id": "li-1234",
    "verdict": "send",
    "reason": "real backend architecture role, Java+K8s core",
    "sponsorship": "implied",
    "rank": 1
  }
]
```

- `verdict`: `"send" | "hold" | "reject"`.
- `reason`: at most ten words. The script truncates rather than rejects an
  over-long reason — a display concern, not a data-integrity one.
- `sponsorship`: `"offered" | "implied" | "doubtful" | "excluded"`, required
  on every entry regardless of verdict, per the 2026-09-03 spec's sponsorship
  section — the field is what makes his one hard deal-breaker auditable.
- `rank`: a positive integer, unique across every `"send"` entry in the
  batch, 1 = best. Used only to order the mechanical selection step; not
  meaningful across `hold`/`reject` entries or across nights.

The model selects from the candidates it was given. It cannot invent a
`job_id` and cannot touch `score`, `score_breakdown`, or any other job-scoring
field — enforced by construction: `record_review` only ever writes the four
new columns below, and only for ids it independently re-verifies as eligible.

### The judgment itself

This is the part a boolean rubric cannot do, and the reason stage 2 exists:
whether a description reads as a real backend architecture/tech-lead role or
a title dressed as one (the corpus already showed this happening — a design
practice hiring building architects, a Full Stack Architect posting that
listed twenty technologies with Java as one among them); whether the company
looks real (a shell posting, a scraped-and-reposted duplicate under a new
name, "AI-first" framing that reads as a red flag rather than a stack
signal); and the sponsorship read, per the 2026-09-03 spec — Gulf employers
sponsor by default and rarely say so, Swiss employers who won't rarely say so
either, so this is a market-and-context judgment, not a phrase match.

The exact prompt wording is a rollout concern (below), not a code artifact —
it lives in the Hermes cron job's `prompt` field, outside this repo.

## Mechanical selection: `select_sendable`

New pure function in `job_scoring.py`, no DB access, matching the module's
existing style:

```python
def select_sendable(reviewed, *, per_market=3, cap=12):
    """Pick which reviewed jobs actually get sent, from the agent's verdicts.

    Everything after the verdict is mechanical and lives outside the model:
    top `per_market` per market by rank, then spill remaining slots to the
    next-best-ranked jobs regardless of market, up to `cap` total.
    """
```

- Input: an iterable of dicts, each already carrying `ai_verdict`,
  `ai_sponsorship`, `ai_rank`, and `market` (derived the same way
  `job_scoring.market_country` already derives it from `location`).
- Filters to `ai_verdict == "send"` and `ai_sponsorship in ("offered", "implied")`
  first — a `send` verdict with a `doubtful`/`excluded` sponsorship read is
  dropped here, not sent, and not marked `rejected` either: sponsorship is
  mostly a market fact, not a per-posting one, so it isn't treated as a
  permanent rejection, it just doesn't clear this round.
- Groups the survivors by market. Within each market, sorts by `ai_rank`
  ascending and keeps the first `per_market`.
- Spillover: whatever didn't make its market's top `per_market`, sorted by
  `ai_rank` ascending across all markets, fills remaining slots up to `cap`.
- Returns the selected jobs, in send order (their `ai_rank`).
- Deterministic and total-order-safe: two jobs can't share a rank inside one
  market's top-N slice without an arbitrary tie-break, so `record_review`
  rejects a batch with a duplicate `rank` among `send` entries before it ever
  reaches this function (see Edge Cases).

## Database

Four new nullable columns on `jobs`, following the existing migration
pattern (`ALTER TABLE ... ADD COLUMN`, try/except `OperationalError`, same
as `credibility_notes` etc.):

```sql
ALTER TABLE jobs ADD COLUMN ai_verdict TEXT DEFAULT ''
ALTER TABLE jobs ADD COLUMN ai_verdict_reason TEXT DEFAULT ''
ALTER TABLE jobs ADD COLUMN ai_sponsorship TEXT DEFAULT ''
ALTER TABLE jobs ADD COLUMN ai_rank INTEGER
```

`status` gains a new value, `'rejected'`, alongside the existing `'new'`,
`'interested'`, `'archived'`. Only an explicit `reject` verdict sets it — the
collector's existing `WHERE ... AND status = 'new'` clause then excludes
rejected jobs from every future night's candidate batch without changing its
shape. `hold` verdicts, and `send` verdicts that lose the cap, both stay
`status='new'`, `notified=0` — they re-compete tomorrow, per the 2026-09-03
spec's "nothing is discarded" delivery rule, which this round's persistence
finally makes real (today, that rule is unenforced: nothing survives to
re-compete because nothing is recorded at all).

## `record_review`

New function in `scraper.py`:

```python
def record_review(conn, verdicts):
    """Persist the agent's verdicts, validated against today's real candidates.

    Returns the list of jobs now eligible for select_sendable: verdict=='send'
    rows, freshly re-read from the database with their written fields.
    """
```

- Re-queries eligible candidates itself — the same `notified=0 AND
  status='new' AND score >= score_threshold` the collector used — rather
  than trusting the batch of ids it's handed. Any `job_id` in `verdicts` not
  in that fresh set is skipped and counted as invalid, not applied.
- Validates `verdict` and `sponsorship` against their fixed enums; an entry
  with a value outside them is skipped and counted as invalid.
- A duplicate `job_id`, or a duplicate `rank` among `send` entries, in the
  same batch: the whole batch is rejected with an error before anything is
  written — see Edge Cases.
- Writes only `ai_verdict`, `ai_verdict_reason` (truncated to 10 words),
  `ai_sponsorship`, `ai_rank` (send entries only; left `NULL` for hold/reject).
  Sets `status='rejected'` for `reject` entries.
- Returns the written `send` rows (each carrying its market, derived via
  `job_scoring.market_country(row["location"])`) for `select_sendable`.

## `jobhunter_review.py`

New Hermes script (`~/.hermes/scripts/`), mirroring `jobhunter_notify.py`'s
existing shape:

```
jobhunter_review.py   (reads a JSON verdicts array on stdin)
```

1. Parse stdin as JSON. A parse failure or non-array payload exits non-zero
   with a message on stderr — the same failure style `jobhunter_notify.py`
   already uses for a bad call.
2. Call `scraper.record_review(conn, verdicts)`.
3. Call `job_scoring.select_sendable(...)` on the result.
4. Send the selected jobs via the existing `scraper.notify_new_jobs` +
   `scraper.mark_notified` — unchanged card format, this round.
5. Print one summary line: sent / held / rejected / invalid counts out of
   total reviewed — the mechanical equivalent of the prompt's current
   "finish with one short line" instruction, but now driven by what was
   actually persisted rather than composed freehand by the agent.

## Edge cases

- **Unknown or stale `job_id`.** Skipped, counted as invalid, reported in
  the summary line. Not fatal — matches `jobhunter_notify.py`'s existing
  "unknown job id(s)" handling.
- **Reason over ten words.** Truncated to ten words, not rejected.
- **Duplicate `job_id` in one batch.** The whole batch is rejected before any
  write — a duplicate signals the agent made a mistake, not a preference to
  arbitrate; better to fail loudly on stderr and let the run be retried or
  reviewed than to silently pick one and hide the other.
- **Duplicate `rank` among `send` entries.** Same treatment — `select_sendable`
  needs a total order within a market's top-N slice, and guessing a tie-break
  hides a genuine agent error.
- **Empty verdicts array.** No-op: 0 written, 0 sent, summary says so.
- **Partial review** (agent stops before covering every candidate it was
  given). Unreviewed jobs are simply absent from `verdicts` — they keep
  `ai_verdict=''`, `status='new'`, `notified=0`, and are reviewed again
  tomorrow. No special casing needed; this falls out of `record_review` only
  ever touching ids actually present in the batch.
- **A market with fewer than 3 sendable jobs.** `select_sendable`'s
  per-market slice is naturally smaller; its unused per-market slots become
  spillover capacity for other markets, per the spec's "spill unused slots"
  rule.

## Rollout

Updating the live Hermes cron job's `prompt` field (and, if Hermes supports
it, its `context`/`skill` linkage) is an operational change on the Pi, made
through Hermes's own tooling — outside this repo, and outside this plan's
task list. This spec fixes the code-side contract `jobhunter_review.py`
expects; the implementer of the rollout step writes the new prompt to match
it and updates `job-hunter.skill.md`'s Stage 2 description in the same pass,
since the skill file is what a future session reads to understand the
workflow. The prompt content itself — the exact judgment instructions from
"The judgment itself" above — is drafted as part of that task, reviewed
against this contract, not treated as a footnote.

Tonight's cron run (2026-09-04T18:00) still fires on the old ad hoc mechanism
— by the user's own choice, made before this spec was written. The swap
happens once this round's implementation is built, tested, and reviewed.

## Acceptance

- `select_sendable` is pure, DB-free, and unit-tested: empty input, exact
  3-per-market, spillover order, the 12 cap, sponsorship filtering, verdict
  filtering, and the "fewer than 3 in a market frees spillover capacity"
  case.
- `record_review` is tested against an in-memory sqlite fixture (matching
  `tests/test_application_tracking.py`'s existing pattern): valid batch
  writes correctly, unknown id is skipped and counted, invalid enum value is
  skipped and counted, duplicate job_id rejects the whole batch, duplicate
  rank among send entries rejects the whole batch, reject verdict sets
  `status='rejected'`, hold verdict leaves `status='new'`/`notified=0`.
- Every existing test still passes (baseline at the start of this work:
  344, per Task 10's completion).
- `job-hunter.skill.md`'s Stage 2 workflow section describes the new
  contract accurately, replacing the current "Hermes cron reviews... keeps
  at most 5" description.

## Out of scope

The digest message format, `show:`/`more:` callbacks, and the top-3-cap's
UI presentation (2026-09-03 spec's "Delivery" section) — a separate design
pass, once this round is live and proven. `feedback_adjusted_score` /
`apply_feedback_learning` — pre-existing, untouched. The stack_fit refit
(Task 9) — still blocked on the user's 60 ratings, unrelated to this work.
