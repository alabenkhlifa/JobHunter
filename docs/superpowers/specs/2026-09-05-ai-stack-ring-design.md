# The AI stack ring

Date: 2026-09-05

## Why

USER DECISION, 2026-09-03 (recorded in the scoring-engine ledger): the AI
stack gets its own ring in `stack_fit`, weighted separately, rather than
joining `ADJACENT_STACK`. 504 corpus postings mention AI/LLM work and their
mean `stack_fit` is 0.17 (median 0.10), because none of `CORE_STACK`,
`CLOUD_STACK`, or `ADJACENT_STACK` contain a single AI-shaped term — while
his master profile lists RAG, LLM integration, and agentic development as
real skills. A posting built entirely around his actual AI experience
currently scores as if he had none of it.

A separate ring — not folding the terms into `ADJACENT_STACK` — avoids the
dilution that adding ~12 terms to that 14-term ring would cause (every
other `ADJACENT_STACK` match would count for less), and makes the AI
weight its own knob: Task 9's eventual refit can set it from his ratings
independently of the other three rings, rather than it being entangled
with unrelated adjacent-stack terms.

Investigating found the extraction side is already done (commit f27d322,
"Teach the extractor the AI stack it could not see") — `CONFIG["tech_terms"]`
in `scraper.py` has carried ~40 AI-shaped terms since then, each counted
against the real corpus before being added. What's missing is entirely on
the scoring side: `job_scoring.py`'s `stack_fit` has no AI ring to credit
any of it, and the 4,580 existing corpus rows were extracted *before*
f27d322 landed, so their `tech_required`/`tech_nice_to_have` columns don't
carry the AI terms even where the description does. Wiring the ring
without re-extracting the corpus would only affect postings scraped after
this change ships — a small, silent gap that would look like "the ring
doesn't work" to anyone measuring against the existing database.

## Scope

Two things: adding `AI_STACK` to `stack_fit`'s ring set, and a one-off
backfill that re-extracts `tech_required`/`tech_nice_to_have` for existing
rows using the current (already-improved) extractor. **Not in scope:**
changing `WEIGHTS["stack"]` (the top-level 35-point dimension weight) or
any other dimension — this only reshapes what's inside `stack_fit`'s own
0.0-1.0 calculation. Not in scope: Task 9's actual refit, which is what
eventually sets the AI ring's real weight from ratings — this spec ships a
placeholder weight explicitly pending that.

## The `AI_STACK` ring

A curated ~10-term ring, not the full ~40-term extraction vocabulary —
matching `CORE_STACK`'s own size (5 terms) and the spec's own reasoning for
why a ring must stay small: `_ring_coverage` divides by ring length, so a
40-term ring would make any single real match (an "LLM integration" posting
that mentions nothing else on the list) count for almost nothing, exactly
re-creating the dilution problem a separate ring exists to avoid.

Proposed terms, canonical forms only (aliases fold the rest — see below):

```python
AI_STACK = (
    "generative ai", "llm", "rag", "prompt engineering", "agentic",
    "ai agent", "ai framework", "vector database", "embeddings", "mcp",
)
```

New `STACK_ALIASES` entries (extending the existing dict, same mechanism
`"k8s": "kubernetes"` already uses):

```python
"genai": "generative ai", "gen ai": "generative ai",
"llms": "llm", "large language model": "llm", "large language models": "llm",
"retrieval-augmented generation": "rag",
"ai agents": "ai agent", "multi-agent": "ai agent",
"vector databases": "vector database",
"model context protocol": "mcp",
"langchain": "ai framework", "langgraph": "ai framework",
"llamaindex": "ai framework", "semantic kernel": "ai framework",
"crewai": "ai framework", "autogen": "ai framework",
```

The six named agent frameworks (LangChain, LangGraph, LlamaIndex, Semantic
Kernel, CrewAI, AutoGen) fold onto one `"ai framework"` canonical term
rather than each getting a ring slot: his skill is agentic development in
general, not any one specific framework brand, and giving each framework
its own ring slot would both bloat the ring past the ~12-term budget the
2026-09-03 decision set and let a posting that happens to name three
framework brands score three times on what is really one signal.

**Before shipping**, the implementer must measure this list against the
corpus the same way every other ring/knockout in this file was measured —
count how many of the 504 AI-mentioning postings each canonical term
actually matches, check for a false-positive class the way `BUILDING_MARKERS`
and the tech extractor's own history did (e.g. does "MCP" collide with
anything else the corpus writes it to mean — a rejected candidate in a
hiring-committee context, say), and report the counts. This spec's term
list is a starting point, not a locked-in inventory; treat it the same way
the original `CORE_STACK`/`CLOUD_STACK` picks were measured, not assumed.

## Wiring into `stack_fit`

Confirmed with the user: the new ring's weight is small and purely
additive — the existing three ring weights (`0.60` core, `0.30` cloud,
`0.10` adjacent) and the `0.62` divisor are **untouched**. The AI ring adds
a fourth term to the numerator only:

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

`0.05` is a placeholder, explicitly: it is not fitted to anything, it is
"a little credit rather than none," chosen so an AI-only posting moves off
the near-zero scores the ledger measured (mean 0.17) without the ring
outweighing the three rings the rubric was actually validated against
(Task 6's AUC measurement predates this ring entirely). Task 9's refit is
what turns this into a real number, the same way it owns the other three.
Because the addition is to the numerator only and the result stays
`min(1.0, ...)`-capped, no existing score can decrease — a posting that
scored optimally on the other three rings still scores 1.0; a posting with
partial coverage elsewhere and full AI coverage may now score marginally
higher than it did before. This is the one-directional, only-adds
guarantee that makes it safe to ship a placeholder weight ahead of the
refit that will replace it.

## Corpus backfill

The 483 corpus postings that mention AI/LLM content but predate f27d322's
extractor improvement need their `tech_required`/`tech_nice_to_have`
columns regenerated from the description already stored, the same shape as
the existing `min_experience` backfill (`data/jobs.db.bak-before-backfill`
is the established precedent for taking a backup first).

A one-off script (not a permanent CLI flag — this runs once, unlike
`--archive-stale-days` which is a recurring operational tool):

```python
# tools/backfill_ai_stack_extraction.py (one-off, not part of the CLI)
import shutil
import sqlite3
import sys
sys.path.insert(0, ".")
import scraper

DB = "data/jobs.db"
BACKUP = "data/jobs.db.bak-before-ai-stack-backfill"

shutil.copy(DB, BACKUP)
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
```

Run once, by hand, after the `AI_STACK` ring itself is reviewed and merged
— not before, so the "before/after" corpus measurement in the task's own
acceptance step is measuring the actual shipped ring, not a guess. The
implementer reports the before/after mean `stack_fit` for the 504
AI-mentioning postings, matching the "before/after" measurement style the
whole scoring-engine plan used throughout (Task 4's employer distribution
shift, Task 7's location-filter row counts, etc.) — a number, not an
opinion, is what makes "did this work" answerable.

## Acceptance

- `AI_STACK` and its aliases are unit-tested: a posting with only
  `tech_required` containing one canonical AI term scores > 0 on the ring;
  a posting with an aliased spelling (e.g. `"GenAI"`, `"CrewAI"`) resolves
  to the same canonical term as the literal spelling; a posting with none
  of the ring's terms is unaffected (same score as before this change,
  proving the addition doesn't regress existing behavior).
- `stack_fit`'s existing test suite (already covering `CORE_STACK`/
  `CLOUD_STACK`/`ADJACENT_STACK`) still passes unchanged — the three
  existing rings' weights and the divisor are untouched by construction.
- The corpus-measurement step (counting each canonical term's real hits,
  checking for false positives) is run and reported before the ring ships,
  matching this project's established practice.
- The backfill script is run once against the real `data/jobs.db` (backed
  up first), and its before/after `stack_fit` measurement for the 504
  AI-mentioning postings is reported.
- Task 6's AUC measurement (`tools/eval_scoring.py`) is re-run after both
  changes land, to confirm the new ring doesn't regress the 0.784/0.772
  raw/freshness-neutral AUC the rubric currently measures at. It is not
  expected to change materially (10 labelled postings, most not AI-shaped),
  but the whole point of this project's practice is measuring rather than
  assuming.

## Out of scope

Task 9's actual weight refit — this ships a placeholder AI ring weight
explicitly pending it. `WEIGHTS["stack"]`, the top-level 35-point dimension
weight — untouched. Any other stack_fit ring's term list — untouched,
this only adds a fourth ring.
