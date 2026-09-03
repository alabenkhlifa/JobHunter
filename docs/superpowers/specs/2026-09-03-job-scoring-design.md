# Job scoring redesign

Date: 2026-09-03

## Why

The current scorer counts keyword hits in the title and `tech_required`,
adds a flat +1 for any nice-to-have match, subtracts 3 for junior-ish
words, and calls anything at or above 15 a match.

Measured against the only ground truth available — 10 jobs marked
`interested` and 34 marked `skipped`, test rows excluded:

| Metric | Current scorer |
|---|---|
| AUC (chance an interested job outranks a skipped one) | **0.565** |
| Mean score, interested | 10.8 |
| Mean score, skipped | 8.8 |
| Interested jobs scoring below the threshold of 15 | **8 of 10** |
| Skipped jobs scoring at or above 15 | 7 of 34 |

A coin flip scores 0.500. The threshold actively hides the jobs the user
wants: `Lead Software Engineer @ Synechron` scores 2, `Solutions
Architect – SkyCargo @ Emirates` scores 9, and both were marked
interested. Meanwhile `software engineer @ Kanz` scores 23 and
`DevOps Manager @ MODSOFT` scores 22.

The failure is structural, not a matter of tuning weights. A bag-of-words
sum has no notion of whether a technology is required or merely named, of
seniority, of who is doing the hiring, or of duplicates.

## Goal

Send at most 3 vetted suggestions per market per day, each justified in a
line, with nothing good silently discarded. The user approves; the
existing tailoring and apply flow is untouched downstream of Interested.

The daily number is a **cap, not a quota**. Supply does not support 12 a
day: over 138 days of history Dubai produced 0.3 jobs per day at or above
the current threshold, Abu Dhabi 0.2, Saudi 0.1. On most days the honest
answer is fewer than three, and the system says so rather than padding.

## Architecture

A two-stage cascade, the standard shape for ranking under a cost
asymmetry: a cheap stage over everything, an expensive stage over the
survivors only.

```
scrape → knockouts → weighted score → [≥45, cap 40] → AI review
       → top 3 per market, spill to 12 → digest → user approves
```

The budget being protected is not money. The agent runs on a ChatGPT
subscription, so the real constraint is the 5-hour and weekly session
caps, which is why stage 2 is bounded by a job count rather than a score
cutoff that drifts with posting volume.

## Stage 1a: knockouts

Boolean, evaluated before any arithmetic, each logged with its reason so
a rejection can be explained. No score can buy a job past these.

1. **Blocked title.** The existing `exclude_terms`. For the infra, QA,
   frontend and data families, seniority words are stripped from the
   title first, so `DevOps Manager`, `DevOps Lead` and `Lead DevOps` all
   collapse to the same block — the gap that let `DevOps Manager` score
   22. The four user-specified entries (`senior architect`, `senior cloud
   architect`, `senior lead software engineer`, `staff engineer`) stay
   literal substrings: stripping "senior" would turn two of them back
   into titles the user wants.
2. **Outside the configured markets.** Unchanged.
3. **Requires local presence or refuses sponsorship.** Unchanged.
4. **Demands more than `max_experience` years.** Unchanged.
5. **Junior, intern, graduate, trainee.** Promoted from a −3 penalty to a
   knockout. As points it is defeatable by buzzword count.
6. **Duplicate.** Same normalised title at the same company inside the
   freshness window. Inception posted one architect role three times in a
   single night; as three rows it would eat three daily slots.

Engineering Manager is deliberately *not* a knockout. The user marked one
interested and skipped another, so that judgment belongs to role fit.

## Stage 1b: the weighted score

Five dimensions, each normalised to [0,1], multiplied by a weight, summing
to 0–100. Weights live in config, not code.

| Dimension | Weight | Scoring |
|---|---|---|
| Stack fit | 35 | Coverage of core (Java, Kotlin, Spring, microservices) and cloud (AWS, Azure, Terraform, Kubernetes) terms, counted from `tech_required`; `tech_nice_to_have` matches are worth a fraction of a required one |
| Role fit | 30 | architect / tech lead 1.0 · senior backend 0.8 · engineering manager 0.5 · full-stack 0.4 · bare "software engineer" 0.3 |
| Seniority | 15 | 5–8 years asked 1.0 · 3–4 0.6 · unstated 0.6 |
| Employer | 12 | Direct employer 1.0 · agency or aggregator repost 0.3 · no company website 0.6 |
| Freshness | 8 | 0–2 days 1.0 · 3–4 0.7 · 5–7 0.4 |

Market was considered and dropped: the user ranks the four equally, so it
would only add noise. Its 10 points went to stack fit and role fit, the
two dimensions the user chose as dominant.

The weights are hand-set, not fitted. Fitting was considered and rejected
for now: 32 feedback labels is far too few. The rubric shape is chosen so
that fitting later is a config change rather than a rewrite. The recorded
skip reasons already validate the dimension set — "wrong stack or weak
backend fit" (6), "low-quality or suspicious posting" (3), "too junior"
(2), "too senior / over-scoped" (1) map onto stack fit, employer, and
role fit respectively.

Bands, used only for display: excellent ≥75 · good 60–74 · normal 45–59 ·
below 45 is never sent.

## The blocking question for Task 9

Stack fit, the heaviest dimension at 35 of 100 points, does not measure what
this table says it measures. `_ring_coverage` asks what fraction of each ring
the posting names, so it rises with the **number of technologies a posting
lists**, not with how central his stack is to the job. A posting that names
twenty technologies covers more of every ring than one that names three,
whatever those technologies are.

Measured over all 4,580 corpus rows, by the count of terms in `tech_required`:

| Terms listed | Rows | Mean stack fit |
|---|---|---|
| 0–3 | 3,301 | 0.04 |
| 4–7 | 816 | 0.23 |
| 8–11 | 265 | 0.38 |
| 12–15 | 144 | 0.54 |
| 16–19 | 34 | 0.73 |
| 20–23 | 20 | 0.90 |

The dimension is close to a monotone function of list length. `Full Stack
Architect @ Xad Technologies` is the worked example: it lists 20 technologies
— Go, C#, Node, GraphQL, Google Cloud and the rest, with Java one item among
them — scores stack 0.95, and totals **78, band "excellent"**, where
acceptance criterion 3 demands under 45.

Task 6 measured the same thing from the other end: stack fit is the *weakest*
single predictor at AUC 0.585, and removing the dimension outright raises
overall AUC from 0.721 to 0.756.

This is deliberately **not** fixed by hand. Reweighting or rewriting stack fit
is exactly the question Task 9's refit exists to settle, and tuning it now
against ten biased labels would replace one guess with another. Two
consequences follow, and they are the reason this section is in the spec
rather than in a ledger line:

1. **The nightly digest must not ship on the current weights.** Stage 1 will
   pass postings whose only qualification is a long technology list, and they
   will reach a real person's Telegram ahead of the roles he wants.
2. **Task 9 is not free to skip the dimension.** Its refit has to decide
   whether stack fit is repaired (count coverage of the ring he is hired for,
   not of every ring), reweighted, or dropped — and to report which, against
   the ratings, not against this corpus.

## Stage 2: the AI review contract

Input: every survivor scoring ≥45, capped at 40 per night. Each carries
title, company, location, required tech, a truncated description, the
five sub-scores, and the feedback learning notes.

Output: strict JSON per job — `verdict` (send / hold / reject), `reason`
of at most ten words, and a rank. The model selects from the list; it
cannot invent entries or overwrite scores.

Its job is the judgment the rubric cannot make: whether a description is
a real backend architecture role or a title dressed as one, whether the
company is real, whether "AI-first" framing is a red flag.

Everything after the verdict is mechanical and lives outside the model:
take the `send` verdicts, top 3 per market, spill unused slots to other
markets up to a global 12.

## Delivery

Jobs not sent stay `notified = 0` and re-compete the next night against
new arrivals, with freshness decayed. `--archive-stale-days` retires
them eventually, so the queue is bounded. Nothing is discarded to make
room.

One digest message per night, replacing the per-job blast:

```
🎯 3 Sep · 4 sent · 6 queued

🇦🇪 DUBAI
1️⃣ Backend Lead – Microservices Architect
   ⭐ 87 · 🏢 PureCS · 🤝 hires directly
   🧩 Kotlin, Spring Boot, K8s · 🗓 2d
   💬 real architecture ownership

🇨🇭 SWITZERLAND
2️⃣ Senior Software Architect – Cloud & API
   ⭐ 79 · 🏢 Bison · 🕵 via a recruiter
   🧩 Java, AWS · 🗓 4d
   💬 Java+AWS core, agency posting

🇸🇦 JEDDAH — nothing today

⤷ 6 more queued (⭐ 71, 68, 66)

[1️⃣] [2️⃣]   [ ➕ Show 5 more ]
```

Icon vocabulary, one meaning each: ⭐ score · 🏢 company · 🧩 required
stack · 🗓 posted · 📈 years wanted · 💰 salary · 💬 why it was picked.
Hiring route is spelled out rather than labelled: 🤝 hires directly ·
🕵 via a recruiter · ♻️ reposted by an aggregator. The earlier draft used
the word "direct", which meant nothing to the reader.

Markets with no matches say so, so a quiet night is distinguishable from
a failed run. Tapping a number expands that job into the existing full
card with Interested/Skip, reusing `format_job_message` and
`job_inline_keyboard`. Two new callback types: `show:<job_id>` and
`more:<date>`.

## Sponsorship: the deal-breaker

Relocation sponsorship is a hard requirement — he applies from Tunisia and
cannot take a role that will not sponsor a work visa. It is therefore the one
dimension allowed to reject a job the rest of the rubric loves.

Measured across 4,575 postings with descriptions:

| The posting | Count | Share |
|---|---|---|
| Offers sponsorship or relocation | 100 | 2.2% |
| Rules it out | 2 | 0.0% |
| Says nothing | 4,472 | **97.7%** |

The rate is flat across every Gulf market (Dubai 2.4%, Abu Dhabi 2.8%, Riyadh
1.5%, Jeddah 1.3%), so no phrase list can carry this requirement. The existing
`local_presence_phrases` knockout matches two postings in the whole corpus.

The silence is the finding, not a gap in the data. In the UAE and Saudi every
foreign hire is on an employer-sponsored work visa by law, so a Gulf employer
advertising publicly is sponsoring by default and has no more reason to say so
than to say it pays a salary. Switzerland is the inverse: a non-EU/EFTA hire
needs a quota-limited third-country permit, and employers who will not do it
rarely say so — they simply do not reply.

So sponsorship risk is mostly a **market** fact, inverted between his four
markets, and only occasionally a posting fact. Three consequences:

1. **It is not a stage-1 knockout.** Gating on text would discard the 97.7% that
   are merely silent, and gating on explicit offers alone would leave roughly two
   jobs a week across every market. The phrase list stays only for the rare
   explicit refusal.

2. **Stage 2 owns the judgment.** The AI already reads the full description, so
   its JSON gains a required `sponsorship` field — `offered`, `implied`,
   `doubtful`, or `excluded` — with the sentence or market fact it relied on.
   `offered` and `implied` pass; `doubtful` and `excluded` are dropped before the
   digest. An LLM catches "you must be eligible to work in Switzerland", which a
   regex over a phrase list does not.

3. **Switzerland stays, flagged.** Swiss postings still come through, but the AI
   must state its sponsorship read for each one explicitly, and employers that
   routinely hire third-country nationals get the benefit of the doubt. The risk
   is shown rather than hidden, and it is his call per job.

The digest shows the sponsorship read on every job, because a deal-breaker the
reader cannot see is not being enforced, only assumed.

## Labelling

The 60-job labelling sample shows each posting's sponsorship position and the
market prior beside it, so the ratings the weights are fitted to are made with
the same information the scorer will have. All 60 in the drawn sample are
silent, which is itself the expected result at a 2.2% disclosure rate.

## Acceptance

The rubric is accepted when, on the 10 interested and 34 skipped jobs:

1. AUC is above 0.75, against the measured baseline of 0.565.
2. At least 8 of the 10 interested jobs reach the stage-1 cutoff of 45
   and so are seen by the AI at all, against 2 of 10 that clear today's
   threshold of 15.
3. `DevOps Manager @ MODSOFT` is knocked out before scoring. **Met** —
   the devops family block catches it.

   This criterion also asked that `software engineer @ Kanz` and
   `Full Stack Architect @ Xad` score below 45. **Neither is met.** Kanz
   scores exactly 45, which is the cutoff, so it would be sent; Xad scores
   78 and bands "excellent". Both fail for the reason set out in "The
   blocking question for Task 9" above. The criterion is recorded as unmet
   rather than quietly relaxed or met by hand-tuning stack fit on ten
   biased labels: Task 9's refit is what has to clear it, and until it does
   the rubric is not accepted and the digest does not ship.

The labelled set is small and was produced under the old scorer, so it
can confirm a rubric is broken but cannot prove one is good. Treat it as
a regression gate, not a proof, and re-measure once feedback reaches a
few hundred labels.

## Out of scope

Automatic submission. The user chose approval-gated suggestions: a bad
auto-application goes out under a real name to a real employer.
