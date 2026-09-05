# AI Stack Ring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `stack_fit` a fourth ring for AI/LLM work (the 2026-09-03
user decision), and re-extract the existing corpus so postings scraped
before the extractor improvement (commit f27d322) actually carry the terms
the new ring reads.

**Architecture:** One new ring constant + alias entries + one new term in
`stack_fit`'s existing weighted sum (`job_scoring.py`). One one-off backfill
script (not a permanent CLI flag) that re-runs the current extractor
against every stored description and updates `tech_required`/
`tech_nice_to_have` in place.

**Tech Stack:** Python 3.13, stdlib only, sqlite3. pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-05-ai-stack-ring-design.md`

## Global Constraints

- `AI_STACK` is a new ~10-term ring in `job_scoring.py`, distinct from
  `CORE_STACK`/`CLOUD_STACK`/`ADJACENT_STACK`. Its exact term list is a
  starting point per the spec — measure against the corpus before
  finalizing (Step 3 of Task 1), the same way every other ring/knockout in
  this file was measured. Do not skip the measurement because the spec
  already lists terms.
- The three existing ring weights (`0.60`/`0.30`/`0.10`) and the `0.62`
  divisor in `stack_fit` are **untouched**. The AI ring adds a fourth
  `+ 0.05 * _ring_coverage(AI_STACK, ...)` term to the numerator only —
  purely additive, one-directional (no existing score can decrease).
  `0.05` is an explicit placeholder pending Task 9's refit, not a fitted
  value — do not treat it as needing justification beyond what the spec
  already gives.
- The backfill script is one-off (`tools/backfill_ai_stack_extraction.py`),
  not a permanent CLI flag — it runs once, against the real `data/jobs.db`,
  after Task 1 is merged, and takes a backup first
  (`data/jobs.db.bak-before-ai-stack-backfill`), matching the existing
  `data/jobs.db.bak-before-backfill` precedent from the min_experience
  backfill.
- Every existing test must still pass: `.venv/bin/python -m pytest -q tests`
  reports 384 passed as of 2026-09-05 (after the digest delivery plan). All
  four of this plan's new `stack_fit` tests were run against the real,
  unmodified `job_scoring.py` before this plan was written (matching this
  session's established practice) — the baseline test's expected value
  (`0.1935483870967742`, Task 1 Step 1) is a real, verified number, not a
  placeholder for the implementer to fill in.

---

### Task 1: `AI_STACK` ring, wired into `stack_fit`

**Files:**
- Modify: `job_scoring.py`
- Test: `tests/test_job_scoring.py` (existing file already covers
  `stack_fit`/`CORE_STACK`/`CLOUD_STACK`/`ADJACENT_STACK` — add to it,
  don't create a new file, this is the same concern the existing tests
  cover)

**Interfaces:**
- Consumes: `_ring_coverage`, `_stack_text`, `STACK_ALIASES`,
  `_ALIAS_PATTERNS` (all already exist in `job_scoring.py`).
- Produces: `AI_STACK: tuple[str, ...]`. `stack_fit`'s return value gains
  the fourth ring term; its signature is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_job_scoring.py`:

```python
def test_ai_stack_ring_credits_a_canonical_term():
    job_without_ai = {"tech_required": "java, spring boot", "tech_nice_to_have": ""}
    job_with_ai = {"tech_required": "java, spring boot, llm, rag", "tech_nice_to_have": ""}
    assert job_scoring.stack_fit(job_with_ai) > job_scoring.stack_fit(job_without_ai)


def test_ai_stack_ring_resolves_aliases_to_the_same_canonical_term():
    literal = {"tech_required": "java, generative ai", "tech_nice_to_have": ""}
    aliased = {"tech_required": "java, genai", "tech_nice_to_have": ""}
    assert job_scoring.stack_fit(literal) == job_scoring.stack_fit(aliased)


def test_ai_stack_ring_folds_named_agent_frameworks_onto_one_canonical_term():
    langchain = {"tech_required": "python, langchain", "tech_nice_to_have": ""}
    crewai = {"tech_required": "python, crewai", "tech_nice_to_have": ""}
    assert job_scoring.stack_fit(langchain) == job_scoring.stack_fit(crewai)


def test_stack_fit_unaffected_when_no_ai_terms_present():
    # The addition must not change scores for postings the AI ring has
    # nothing to say about -- proves this is additive, not a regression.
    # A job listing only "java" (not a richer stack) is deliberate: a
    # richer required list saturates stack_fit at 1.0 via the CORE_STACK/
    # substring-matching quirk already known in this file ("spring"
    # matches inside "spring boot"), which would make this test pass
    # trivially regardless of whether the AI ring is wired correctly.
    # Verified against the real, unmodified job_scoring.py before this
    # task existed: stack_fit({"tech_required": "java", ...}) == 0.1935483870967742.
    job = {"tech_required": "java", "tech_nice_to_have": ""}
    assert job_scoring.stack_fit(job) == 0.1935483870967742
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v -k ai_stack`
Expected: the first three new tests FAIL with `AttributeError` or similar
(`AI_STACK` doesn't exist yet); the fourth (baseline) test should PASS
already, since it asserts against current, unmodified behavior — if it
does not pass before you've changed anything, stop and re-check the
`data/jobs.db` / `job_scoring.py` state before proceeding, since the
expected value was captured against a specific known-good state.

- [ ] **Step 3: Measure the term list against the real corpus**

Run this before finalizing the ring — it is a measurement, not a
formality, per every prior task's practice in this codebase:

```bash
.venv/bin/python - <<'PY'
import re
import sqlite3

AI_STACK = ("generative ai", "llm", "rag", "prompt engineering", "agentic",
            "ai agent", "ai framework", "vector database", "embeddings", "mcp")
ALIASES = {
    "genai": "generative ai", "gen ai": "generative ai",
    "llms": "llm", "large language model": "llm", "large language models": "llm",
    "retrieval-augmented generation": "rag",
    "ai agents": "ai agent", "multi-agent": "ai agent",
    "vector databases": "vector database",
    "model context protocol": "mcp",
    "langchain": "ai framework", "langgraph": "ai framework",
    "llamaindex": "ai framework", "semantic kernel": "ai framework",
    "crewai": "ai framework", "autogen": "ai framework",
}
conn = sqlite3.connect("data/jobs.db")
rows = conn.execute("SELECT description FROM jobs WHERE description != ''").fetchall()
counts = {term: 0 for term in AI_STACK}
for (desc,) in rows:
    text = desc.lower()
    for alias, canonical in ALIASES.items():
        text = re.sub(rf"\b{re.escape(alias)}\b", canonical, text)
    for term in AI_STACK:
        if re.search(rf"\b{re.escape(term)}\b", text):
            counts[term] += 1
for term, count in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f"{count:5d}  {term}")
print(f"\n{sum(1 for (d,) in rows if any(t in d.lower() for t in ('llm','rag','ai agent','generative ai')))} rows mention at least one obvious AI term (rough check)")
PY
```

Read the output. If any term has zero hits, or a term's hits look like a
false-positive class (e.g. "mcp" matching something unrelated to Model
Context Protocol), report it rather than silently dropping or keeping it —
this is exactly the kind of finding this project's ledger records as a
ruling, not something to decide unilaterally mid-implementation.

- [ ] **Step 4: Write the implementation**

Add to `job_scoring.py`, directly after `ADJACENT_STACK`:

```python
# The AI stack: confirmed with him as its OWN ring, not folded into
# ADJACENT_STACK, because 504 corpus postings mention AI/LLM work with a
# mean stack_fit of 0.17 despite his master profile listing RAG, LLM
# integration, and agentic development as real skills -- none of the other
# three rings contain a single AI-shaped term. Kept small (~10 terms,
# matching CORE_STACK's own scale) rather than the ~40-term extraction
# vocabulary in scraper.py's CONFIG["tech_terms"]: _ring_coverage divides
# by ring length, so a 40-term ring would make one real match count for
# almost nothing -- the exact dilution problem a separate ring exists to
# avoid.
AI_STACK = (
    "generative ai", "llm", "rag", "prompt engineering", "agentic",
    "ai agent", "ai framework", "vector database", "embeddings", "mcp",
)
```

Extend the existing `STACK_ALIASES` dict (do not create a second dict):

```python
STACK_ALIASES = {
    "k8s": "kubernetes",
    "genai": "generative ai", "gen ai": "generative ai",
    "llms": "llm", "large language model": "llm", "large language models": "llm",
    "retrieval-augmented generation": "rag",
    "ai agents": "ai agent", "multi-agent": "ai agent",
    "vector databases": "vector database",
    "model context protocol": "mcp",
    # Six named agentic frameworks fold onto one canonical term: his skill
    # is agentic development in general, not any one framework brand, and
    # giving each its own ring slot would both bloat the ring past its
    # ~12-term budget and let one posting score multiple times on what is
    # really one signal.
    "langchain": "ai framework", "langgraph": "ai framework",
    "llamaindex": "ai framework", "semantic kernel": "ai framework",
    "crewai": "ai framework", "autogen": "ai framework",
}
```

In `stack_fit`, add the fourth term:

```python
def stack_fit(job):
    """How central his stack is to the posting, 0.0-1.0."""
    required = _stack_text(job.get("tech_required"))
    optional = _stack_text(job.get("tech_nice_to_have"))
    return min(1.0, (
        0.60 * _ring_coverage(CORE_STACK, required, optional)
        + 0.30 * _ring_coverage(CLOUD_STACK, required, optional)
        + 0.10 * _ring_coverage(ADJACENT_STACK, required, optional)
        + 0.05 * _ring_coverage(AI_STACK, required, optional)
    ) / 0.62)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v -k "ai_stack or stack_fit"`
Expected: PASS, all new and pre-existing `stack_fit`-related tests green.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, no regressions against whatever baseline Step 2 recorded.

- [ ] **Step 7: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Give stack_fit a fourth ring for AI/LLM work

Confirmed with him, 2026-09-03: the AI stack gets its own ring rather
than joining ADJACENT_STACK, since folding ~12 terms in would dilute
every other adjacent-stack match. 504 corpus postings mention AI/LLM
work with a mean stack_fit of 0.17 despite it being a real skill on
his master profile. Weight (0.05) is an explicit placeholder pending
Task 9's refit, additive only -- no existing score can decrease."
```

---

### Task 2: Backfill the corpus, measure the effect

**Files:**
- Create: `tools/backfill_ai_stack_extraction.py` (one-off, not a
  permanent CLI flag)

**Interfaces:**
- Consumes: `scraper.extract_tech_keywords` (already exists), the real
  `data/jobs.db`.
- Produces: no new function signature — this is a one-off data migration,
  run once by hand, not integrated into any test suite.

- [ ] **Step 1: Back up the real database**

```bash
cp data/jobs.db data/jobs.db.bak-before-ai-stack-backfill
```

- [ ] **Step 2: Write the backfill script**

```python
# tools/backfill_ai_stack_extraction.py
"""One-off: re-extract tech_required/tech_nice_to_have for every stored
description, using the current extractor. Run once, after the AI stack
ring (Task 1 of this plan) is merged -- not before, so the "did this
help" measurement below reflects the shipped ring, not a guess.

Needed because commit f27d322 improved scraper.extract_tech_keywords to
recognize AI/LLM vocabulary, but the 4,580 rows already in data/jobs.db
were extracted before that commit landed -- their stored tech fields
don't carry the AI terms even where the description does.
"""
import sqlite3
import sys
sys.path.insert(0, ".")
import scraper

DB = "data/jobs.db"


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, description FROM jobs WHERE description != ''").fetchall()

    changed = 0
    for row in rows:
        required, nice = scraper.extract_tech_keywords(row["description"])
        req_str, nice_str = ", ".join(required), ", ".join(nice)
        current = conn.execute(
            "SELECT tech_required, tech_nice_to_have FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        if current["tech_required"] != req_str or current["tech_nice_to_have"] != nice_str:
            conn.execute(
                "UPDATE jobs SET tech_required = ?, tech_nice_to_have = ? WHERE id = ?",
                (req_str, nice_str, row["id"]),
            )
            changed += 1
    conn.commit()
    print(f"backfilled {changed} of {len(rows)} rows")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Measure stack_fit before running the backfill**

```bash
.venv/bin/python - <<'PY'
import sqlite3
import sys
sys.path.insert(0, ".")
import job_scoring

conn = sqlite3.connect("data/jobs.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT tech_required, tech_nice_to_have, description FROM jobs").fetchall()
ai_terms = ("llm", "rag", "generative ai", "agentic", "ai agent", "genai")
ai_rows = [r for r in rows if r["description"] and any(t in r["description"].lower() for t in ai_terms)]
scores = [job_scoring.stack_fit(dict(r)) for r in ai_rows]
print(f"{len(ai_rows)} AI-mentioning rows, mean stack_fit BEFORE backfill: {sum(scores)/len(scores):.3f}")
PY
```

Record this number — it's the "before" half of the acceptance measurement.

- [ ] **Step 4: Run the backfill**

```bash
.venv/bin/python tools/backfill_ai_stack_extraction.py
```

Expected: prints `backfilled N of M rows`. Report N.

- [ ] **Step 5: Measure stack_fit after the backfill**

Re-run Step 3's script against the now-updated `data/jobs.db`. Report the
new mean `stack_fit` for the same AI-mentioning rows. It should be
materially higher than Step 3's number — if it isn't, something is wrong
(the ring's terms don't match what the extractor actually wrote, or the
backfill didn't run against the terms the ring reads) and must be
investigated before this task is called done, not reported as-is.

- [ ] **Step 6: Re-run the AUC measurement**

```bash
.venv/bin/python tools/eval_scoring.py
```

Report the `auc` / `auc_freshness_neutral` figures against the
0.784/0.772 baseline measured before this plan. Confirm they haven't
regressed (10 labelled postings is a small, mostly-non-AI sample, so a
material *improvement* isn't expected either — the point of re-running
this is to catch an unintended regression, not to claim a win).

- [ ] **Step 7: Commit**

```bash
git add tools/backfill_ai_stack_extraction.py
git commit -m "Backfill tech extraction for the corpus predating the AI stack

The 4,580 rows in data/jobs.db were extracted before commit f27d322
added AI/LLM vocabulary to the extractor. One-off re-extraction so the
new AI_STACK ring (previous commit) actually has something to read on
existing rows, not just newly-scraped ones going forward."
```

Note: `data/jobs.db` is gitignored — this commit is the script only. The
backup file (`data/jobs.db.bak-before-ai-stack-backfill`) stays local,
same as the existing `data/jobs.db.bak-before-backfill` precedent.

---

## What this plan does not cover

Task 9's actual weight refit, which is what eventually replaces the `0.05`
placeholder with a real, ratings-derived weight — still blocked on the
user's ratings (only 25 exist, 2 positive). Syncing this change to the
Pi — a separate manual step, same shape as the Stage 2 and digest delivery
rollouts (`rsync` the tracked files, matching the established pattern from
earlier this session), not included here since it depends on when this
plan actually gets executed relative to those.
