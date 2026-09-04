# Digest delivery

Date: 2026-09-04

## Why

The 2026-09-03 spec's "Delivery" section specced a single consolidated
nightly digest replacing the per-job Telegram blast, with drill-down via
`show:<job_id>` and `more:<date>` callback buttons. The 2026-09-04 Stage 2
spec explicitly deferred this as a separate design pass, and shipped with
today's per-job cards unchanged.

Before designing the digest, investigating found the button-drill-down half
of that original design cannot work as specced. `job_inline_keyboard`
produces `callback_data` values like `interested:<id>`, `skip:<id>`,
`skip_reason:<code>:<id>` — and nothing in production handles them.
`callback_handler.py`, the script built to process these, is not running
anywhere on the Pi (no process, no systemd unit). Hermes's own Telegram
adapter (`~/.hermes/hermes-agent/plugins/platforms/telegram/adapter.py`,
`_handle_callback_query`) is the only thing actually polling Telegram for
callback queries, and it dispatches on a fixed, exhaustive set of its own
prefixes (`mp:`, `cp:`, `gt:`, `ea:`, `sc:`, `cl:`, `update_prompt:` — all
Hermes's own UI flows: model picker, exec approval, Gmail triage, etc.).
Anything else, including every prefix `job_inline_keyboard` produces, hits
`if not data.startswith("update_prompt:"): return` and is silently dropped.

Confirmed directly with the user: what actually works today is replying
with the word "interested" as an ordinary chat message, which
`job-hunter.skill.md`'s own trigger list (`interested in job`) picks up
through Hermes's normal message handling — not the button.

This spec replaces the button-driven drill-down with a text-reply model
that uses the same proven path, and keeps everything else from the
2026-09-03 mockup (grouping, icon vocabulary, "nothing today" markets,
queued count).

## Scope

The digest message format, the code that sends it, and the skill
instructions for interpreting a reply to it. **Not in scope:** reviving
`callback_handler.py`, extending Hermes's adapter, or any other path that
would make the original button design work — the recommended and chosen
direction is to not need buttons at all.

## Digest format

```
🎯 4 Sep · 4 sent · 6 queued

🇦🇪 DUBAI
1️⃣ Backend Lead – Microservices Architect
   ⭐ 87 · 🏢 PureCS · 🤝 hires directly · 🛂 sponsorship implied
   🧩 Kotlin, Spring Boot, K8s · 🗓 2d
   💬 real architecture ownership

🇨🇭 SWITZERLAND
2️⃣ Senior Software Architect – Cloud & API
   ⭐ 79 · 🏢 Bison · 🕵 via a recruiter · 🛂 sponsorship offered
   🧩 Java, AWS · 🗓 4d
   💬 Java+AWS core, agency posting

🇸🇦 JEDDAH — nothing today
🇦🇪 ABU DHABI — nothing today
🇸🇦 RIYADH — nothing today

⤷ 6 more queued (⭐ 71, 68, 66)

Reply with a number to see the full listing, or "more" to see what's queued.
```

- Header: date, sent count, queued count (see "Queued", below).
- One numbered entry per sent job, grouped under its market's header
  (fixed market order — see below), sorted by `ai_rank` within each
  market's block. The displayed number is a fresh sequential label in that
  final top-to-bottom display order (1, 2, 3, ... with no gaps) — **not**
  a copy of `ai_rank` itself, which can have gaps (a market's other
  candidates that didn't clear the cap) and doesn't respect market
  grouping. A reply of "2" must mean "the second job as printed, reading
  top to bottom" — anything else risks the reader replying with a number
  that visually doesn't match what they're looking at.
- Every one of the five configured markets gets a header line. A market
  with no sends that night still appears, suffixed `— nothing today`, per
  the 2026-09-03 spec's explicit requirement that a quiet market be
  distinguishable from a failed run.
- Icon line 1: `⭐` score (the job_scoring 0-100 total, unchanged from
  today's cards) · `🏢` company · hiring route · `🛂` sponsorship read.
- **Hiring route** is `🤝 hires directly` when `job_scoring.employer_fit(job)
  == DIRECT_EMPLOYER`, else `🕵 via a recruiter`. The 2026-09-03 mockup's
  third icon (`♻️ reposted by an aggregator`) is dropped: `employer_fit` is a
  two-tier classification in the shipped code (direct/agency), and no
  distinct "aggregator" signal exists to hang a third icon on. Inventing one
  here would be decoration the underlying data can't back up.
- **Sponsorship** (`🛂`) is new relative to the 2026-09-03 mockup, which
  didn't show it despite that same spec's sponsorship section requiring it
  ("the digest shows the sponsorship read on every job, because a
  deal-breaker the reader cannot see is not being enforced, only assumed").
  Sourced directly from `ai_sponsorship`, already persisted by
  `record_review`. Only `offered`/`implied` jobs ever reach the digest
  (`select_sendable` already filters `doubtful`/`excluded` out), so this is
  display, not a new gate.
- Icon line 2: `🧩` required tech (`tech_required`, unchanged from today's
  cards) · `🗓` posted age (`job_age`, unchanged).
- `💬` is `ai_verdict_reason` — the agent's own ten-word justification,
  already persisted. No new data.
- **Queued**: a count of jobs that exist right now with `status='new'`,
  `notified=0`, `score >= score_threshold` (the same population
  `record_review` re-queries as "eligible"), plus the top three scores
  among them, parenthetically. This is a live count at send time, not a
  historical one — it answers "what's sitting in the queue after tonight's
  selection," which is what the reader wants to know.
- Closing line: plain-text instructions, replacing the mockup's button row.

## New code

### `format_digest_message(sent, queued_count, queued_top_scores)`

Pure function in `scraper.py`, beside `format_job_message`. `sent` is a
list of full job row dicts in any order, each already carrying a `market`
key (added by `send_digest` before calling this — see below) alongside the
`ai_verdict_reason`/`ai_sponsorship` columns `record_review` already wrote.
The function does its own ordering: groups by market, sorts each market's
jobs by `ai_rank`, then assigns sequential display numbers 1..N in that
final order (see the numbering rule above) — the caller does not pre-sort.
Market header order is a new `DIGEST_MARKET_ORDER = ("dubai", "abu dhabi",
"jeddah", "riyadh", "switzerland")` constant — lowercase to match
`market_region`'s return values directly (`CONFIG["regions"]`'s keys are
capitalized, a different casing convention for a different purpose, so
this is its own constant rather than a derived one), in the same order
`CONFIG["regions"]` defines them. A market with zero entries still gets a
header line, suffixed "— nothing today". Returns the full message text; no
`reply_markup` — this message carries no keyboard.

### `send_digest(token, chat_id, conn, selected)`

In `scraper.py`, beside `notify_new_jobs`. `selected` is the list of full
job row dicts — the same `SELECT * FROM jobs WHERE id IN (...)` result
`jobhunter_review.py` already builds today for `notify_new_jobs` (Stage 2's
`record_review` wrote `ai_verdict_reason`/`ai_sponsorship` as real columns
on `jobs`, so a plain `SELECT *` already carries them; `market` is the one
field that isn't a column, and `send_digest` computes it itself per row via
`job_scoring.market_region(row["location"])` before handing the list to
`format_digest_message` — callers never compute or pass `market`).

Queries the queued count/top scores, calls `format_digest_message`, sends
the one message via `send_telegram`, then calls `mark_notified(conn, [job
ids])` for the sent jobs — same persistence step `notify_new_jobs` already
does, just once instead of per-job.

### `--list-queued` CLI flag

New flag on `scraper.py`'s existing argparse CLI, beside `--get-job`.
Prints the top N (default 10, override with a value) queued jobs — same
population as the digest's queued count — as JSON to stdout: title,
company, market, score. Mirrors `--get-job`'s existing shape (a plain
on-demand query the agent invokes conversationally, not a scheduled
script), used when the user replies "more".

### `jobhunter_review.py`

Existing script (from the Stage 2 plan). Its final steps — build the SELECT
WHERE IN query, call `notify_new_jobs`, call `mark_notified` — are replaced
with one call to `send_digest`. The rest of the script (parsing stdin,
`record_review`, `select_sendable`) is unchanged.

### `job-hunter.skill.md`

New instructions, replacing whatever the Stage 2 rollout's Step 3 wrote
about sending "the existing per-job Telegram card":

- The nightly digest is one message, numbered per job. A bare numeric
  reply that follows it (e.g. "2") refers to that job — resolved from the
  agent's own memory of the digest it just sent, not from any stored
  mapping. Look up that job's id, run `scraper.py --get-job <id>`, and
  continue into the existing tailoring workflow exactly as if the user had
  named the job directly.
- A reply of "more" runs `scraper.py --list-queued` and presents the
  result as a short follow-up text list — not a second digest, not
  numbered for further drill-down (avoids nesting the same ambiguity
  problem one level deeper).
- The existing "interested"/"skip" triggers are unchanged: once a
  specific job is in view (from a numbered reply or otherwise), those
  words work exactly as documented today.

## Acceptance

- `format_digest_message` is pure, DB-free, unit-tested: empty `sent`
  list (all five markets "nothing today"), one job, multiple jobs across
  multiple markets in rank order, the hiring-route icon for both
  `employer_fit` tiers, the sponsorship icon for `offered` and `implied`,
  and the case that actually distinguishes the numbering rule from a bare
  copy of `ai_rank` — a market earlier in `DIGEST_MARKET_ORDER` (e.g.
  Dubai) whose job has a *worse* (higher) `ai_rank` than a job in a later
  market (e.g. Switzerland) must still be numbered 1 because it's printed
  first.
- `send_digest` is tested against an in-memory sqlite fixture (matching
  `tests/test_review_recording.py`'s pattern): sends one message (not
  N), marks exactly the selected jobs notified, queued count/top-scores
  reflect the real eligible population.
- `--list-queued` is smoke-tested: valid JSON, respects a `--limit`
  override, empty queue prints an empty array rather than erroring.
- Every existing test still passes.
- `job-hunter.skill.md` accurately describes the new flow, replacing the
  stale "existing per-job Telegram card" line from the Stage 2 rollout.

## Out of scope

Reviving `callback_handler.py` or extending Hermes's adapter — the button
path is abandoned, not fixed. Multi-level drill-down from "more" (the
queued list is presented flat, not itself numbered for further
expansion). Archiving/pruning policy for the queue — `--archive-stale-days`
already exists and is unchanged. Task 9's weight refit and the stack_fit
question — unrelated, still blocked on the user's ratings.
