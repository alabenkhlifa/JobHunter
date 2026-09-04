# Stage 2 AI Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the already-live but ad hoc Hermes AI review a strict per-job
JSON contract, persist its verdicts, and add the mechanical top-3-per-market-
with-spillover-to-12 selection the 2026-09-03 spec called for.

**Architecture:** A new pure selection function in `job_scoring.py`
(`select_sendable`), a new DB-backed persistence function in `scraper.py`
(`record_review`) with four new nullable columns, and an accurate rewrite of
`job-hunter.skill.md`'s Stage 2 description. The Hermes script that calls
these two functions on the Pi (`jobhunter_review.py`) and the Hermes cron
prompt itself live outside this repo (`~/.hermes/scripts/`,
`~/.hermes/cron/jobs.json`) and are explicitly **out of this plan's task
list** — a manual rollout step once these three tasks are built and reviewed.

**Tech Stack:** Python 3.13, stdlib only, sqlite3. pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-04-stage-2-ai-review-design.md`

## Global Constraints

- `select_sendable(reviewed, *, per_market=3, cap=12)` in `job_scoring.py`.
  No DB access, no import of `scraper.py` (existing project-wide rule).
- `verdict` enum: `"send" | "hold" | "reject"`. `sponsorship` enum:
  `"offered" | "implied" | "doubtful" | "excluded"`. `ai_rank`: positive int,
  unique across every `"send"` entry in one batch.
- Each market's own top `per_market` is a **floor**, not an absolute ceiling:
  confirmed with the user directly, a market that is thin or absent leaves
  capacity unused, and that capacity spills to whichever other market has
  more good candidates — a single market can use the whole `cap` if every
  other market is empty. The only case a market is held to exactly
  `per_market` is when the *floor itself* (every market's own top
  `per_market`, before any spillover) already reaches or exceeds `cap` — then
  it is truncated to the global top `cap` by `ai_rank` and spillover never
  runs at all, since no capacity is left unused anywhere. See the spec's
  "Mechanical selection" section (Task 7 added a fifth market after the
  original 3×4=12 arithmetic was written, so this case is now reachable).
- New columns on `jobs`, following the existing migration pattern (`ALTER
  TABLE jobs ADD COLUMN ...`, try/except `sqlite3.OperationalError`, same as
  `credibility_notes` etc. in `scraper.py`'s `init_db`):
  `ai_verdict TEXT DEFAULT ''`, `ai_verdict_reason TEXT DEFAULT ''`,
  `ai_sponsorship TEXT DEFAULT ''`, `ai_rank INTEGER`.
- `status` gains a new value, `'rejected'`, alongside the existing `'new'`,
  `'interested'`, `'archived'`.
- `record_review(conn, verdicts)` in `scraper.py`. Re-queries eligible
  candidates itself (`notified=0 AND status='new' AND score >=
  score_threshold`) rather than trusting the batch it's handed. Writes only
  the four new columns — never `score` or `score_breakdown`.
- Every existing test must still pass: `.venv/bin/python -m pytest -q tests`
  currently reports 344 passed (baseline after Task 10 of the scoring-engine
  plan).

---

### Task 1: `market_region` and the mechanical selection — `select_sendable`

**Files:**
- Modify: `job_scoring.py`
- Test: `tests/test_select_sendable.py` (new file — `test_job_scoring.py` is
  already large, and this is a distinct concern from knockouts/scoring)

**Interfaces:**
- Consumes: `MARKET_COUNTRIES` (defined in Task 7 of the scoring-engine
  plan, already in `job_scoring.py`) — reused, not duplicated, for the
  Swiss city/language spellings and the `jiddah` transliteration.
- Produces: `market_region(location: str) -> str`, one of `"dubai"`,
  `"abu dhabi"`, `"jeddah"`, `"riyadh"`, `"switzerland"`, or `"unknown"`.
  **Not** the existing `market_country` — that groups by country
  (`market_country("Dubai")` and `market_country("Abu Dhabi")` both return
  `"uae"`), which would silently merge two of the five markets for
  selection purposes. The 2026-09-03 spec's own digest mockup shows
  `🇦🇪 DUBAI` and `🇨🇭 SWITZERLAND` as separate market headers, and its Goal
  section gives Dubai and Abu Dhabi separate daily-supply figures — markets
  here are cities, except Switzerland, which stays one market by the
  original scraping design (a country-wide query, not a city list).
- Produces: `select_sendable(reviewed: list[dict], *, per_market: int = 3, cap: int = 12) -> list[dict]`.
  Each input dict is expected to carry `ai_verdict`, `ai_sponsorship`,
  `ai_rank`, and `market` (the output of `market_region`) keys. Returns the
  selected subset, in `ai_rank` order (ascending — lower rank first).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_select_sendable.py
import job_scoring


def test_market_region_finds_each_of_the_five_markets():
    assert job_scoring.market_region("Dubai, United Arab Emirates") == "dubai"
    assert job_scoring.market_region("Abu Dhabi, United Arab Emirates") == "abu dhabi"
    assert job_scoring.market_region("Jeddah, Saudi Arabia") == "jeddah"
    assert job_scoring.market_region("Jiddah, Makkah, Saudi Arabia") == "jeddah"
    assert job_scoring.market_region("Riyadh, Saudi Arabia") == "riyadh"
    assert job_scoring.market_region("Zürich, Switzerland") == "switzerland"
    assert job_scoring.market_region("Geneva, Switzerland") == "switzerland"


def test_market_region_does_not_merge_dubai_and_abu_dhabi():
    # The bug this function exists to avoid: market_country groups both
    # under "uae". market_region must not.
    assert job_scoring.market_region("Dubai") != job_scoring.market_region("Abu Dhabi")


def test_market_region_is_unknown_for_a_bare_country_or_unplaced_location():
    assert job_scoring.market_region("United Arab Emirates") == "unknown"
    assert job_scoring.market_region("Saudi Arabia") == "unknown"
    assert job_scoring.market_region("") == "unknown"
    assert job_scoring.market_region(None) == "unknown"


def review(**over):
    base = {
        "id": "j1", "market": "dubai",
        "ai_verdict": "send", "ai_sponsorship": "implied", "ai_rank": 1,
    }
    base.update(over)
    return base


def test_select_sendable_returns_nothing_for_empty_input():
    assert job_scoring.select_sendable([]) == []


def test_select_sendable_filters_out_non_send_verdicts():
    jobs = [
        review(id="hold", ai_verdict="hold"),
        review(id="reject", ai_verdict="reject"),
        review(id="send", ai_verdict="send"),
    ]
    result = job_scoring.select_sendable(jobs)
    assert [j["id"] for j in result] == ["send"]


def test_select_sendable_filters_out_doubtful_and_excluded_sponsorship():
    jobs = [
        review(id="doubtful", ai_sponsorship="doubtful"),
        review(id="excluded", ai_sponsorship="excluded"),
        review(id="offered", ai_sponsorship="offered"),
        review(id="implied", ai_sponsorship="implied"),
    ]
    result = job_scoring.select_sendable(jobs)
    assert {j["id"] for j in result} == {"offered", "implied"}


def test_select_sendable_holds_a_market_to_its_floor_when_others_use_their_full_share():
    # Dubai has 5 candidates but Jeddah is using its own full 3-slot share,
    # so there is no unused capacity anywhere for Dubai's 4th/5th to spill
    # into -- the floor holds exactly at per_market for both.
    jobs = [review(id=f"dubai-{i}", market="dubai", ai_rank=i) for i in range(1, 6)]
    jobs += [review(id=f"jeddah-{i}", market="jeddah", ai_rank=i) for i in range(6, 9)]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=6)
    assert [j["id"] for j in result] == [
        "dubai-1", "dubai-2", "dubai-3", "jeddah-6", "jeddah-7", "jeddah-8",
    ]


def test_select_sendable_lets_one_market_exceed_the_floor_when_every_other_is_empty():
    # Confirmed design: a market's own unused capacity flows to whichever
    # market has more candidates. With every other market absent, all of
    # their unused slots are available, so Dubai can use the whole cap.
    jobs = [review(id=f"dubai-{i}", market="dubai", ai_rank=i) for i in range(1, 6)]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert [j["id"] for j in result] == [f"dubai-{i}" for i in range(1, 6)]


def test_select_sendable_spills_remaining_slots_to_other_markets_by_rank():
    jobs = [
        review(id="dubai-1", market="dubai", ai_rank=1),
        review(id="dubai-2", market="dubai", ai_rank=2),
        review(id="jeddah-1", market="jeddah", ai_rank=3),
    ]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert [j["id"] for j in result] == ["dubai-1", "dubai-2", "jeddah-1"]


def test_select_sendable_caps_total_at_the_global_limit_via_spillover():
    # 4 markets x 2 jobs each = 8, all within each market's top 3, so every
    # job clears the per-market floor. Cap trims the spillover, not the floor.
    jobs = []
    rank = 1
    for market in ("dubai", "abu dhabi", "jeddah", "switzerland"):
        for _ in range(2):
            jobs.append(review(id=f"{market}-{rank}", market=market, ai_rank=rank))
            rank += 1
    result = job_scoring.select_sendable(jobs, per_market=3, cap=6)
    assert len(result) == 6
    assert [j["ai_rank"] for j in result] == [1, 2, 3, 4, 5, 6]


def test_select_sendable_truncates_the_per_market_floor_itself_when_it_exceeds_the_cap():
    # 5 markets x 3 jobs each = 15 sendable jobs, every one inside its own
    # market's top 3 -- the floor alone exceeds cap=12. No market may exceed
    # 3, but the overall list truncates to the global top 12 by rank.
    jobs = []
    rank = 1
    for market in ("dubai", "abu dhabi", "jeddah", "switzerland", "riyadh"):
        for _ in range(3):
            jobs.append(review(id=f"{market}-{rank}", market=market, ai_rank=rank))
            rank += 1
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert len(result) == 12
    ranks = [j["ai_rank"] for j in result]
    assert ranks == sorted(ranks)
    assert ranks[-1] == 12
    counts = {}
    for j in result:
        counts[j["market"]] = counts.get(j["market"], 0) + 1
    assert all(count <= 3 for count in counts.values())


def test_select_sendable_a_thin_market_frees_spillover_capacity():
    jobs = [
        review(id="dubai-1", market="dubai", ai_rank=1),
        # jeddah has only 1 sendable job -- its other 2 slots are unused,
        # not blocked, so a lower-ranked dubai job can spill in.
        review(id="jeddah-1", market="jeddah", ai_rank=2),
        review(id="dubai-2", market="dubai", ai_rank=3),
        review(id="dubai-3", market="dubai", ai_rank=4),
        review(id="dubai-4", market="dubai", ai_rank=5),
    ]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    ids = {j["id"] for j in result}
    assert ids == {"dubai-1", "dubai-2", "dubai-3", "jeddah-1", "dubai-4"}


def test_select_sendable_returns_jobs_in_rank_order():
    jobs = [
        review(id="third", market="dubai", ai_rank=3),
        review(id="first", market="jeddah", ai_rank=1),
        review(id="second", market="switzerland", ai_rank=2),
    ]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert [j["id"] for j in result] == ["first", "second", "third"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_select_sendable.py -v`
Expected: FAIL with `AttributeError: module 'job_scoring' has no attribute 'market_region'`
(the `market_region` tests run first and fail first; once that attribute
exists, the `select_sendable` tests below will fail the same way on
`select_sendable` until Step 3 is complete)

- [ ] **Step 3: Write the implementation**

Add to `job_scoring.py`, right after `market_country` (reuses
`MARKET_COUNTRIES` directly, so it must come after that definition):

```python
# Which of his five markets a location falls in, for stage-2 selection --
# NOT market_country, which groups Dubai and Abu Dhabi together as "uae"
# for duplicate_key's purposes. The digest's markets are cities: the
# 2026-09-03 spec's own mockup shows Dubai and Switzerland as separate
# market headers. Switzerland stays one market, unsplit -- a country-wide
# scrape query by original design, not a city list.
_REGION_OF_TERM = {
    "dubai": "dubai", "abu dhabi": "abu dhabi",
    "jeddah": "jeddah", "jiddah": "jeddah", "riyadh": "riyadh",
}


def market_region(location):
    """Which of his five markets a displayed location falls in.

    Matches the same way market_country and knockout do. "unknown" when
    nothing places it -- same fallback rule as market_country.
    """
    location = str(location or "").lower()
    for term, region in _REGION_OF_TERM.items():
        if term in location:
            return region
    if any(term in location for term in MARKET_COUNTRIES["ch"]):
        return "switzerland"
    return "unknown"
```

Then, near `duplicate_key`/`knockout` (the module's other DB-row-shaped,
non-scoring functions):

```python
# Stage 2 (the AI review) hands back a verdict/sponsorship/rank per job;
# everything after that is mechanical and lives here, not in the model.
# `per_market` is each market's own FLOOR, not a hard ceiling: confirmed
# directly with him, a thin or absent market's unused capacity flows to
# whichever other market has more good candidates -- a single market with
# every other one empty can use the whole `cap`. The exception is when the
# floor ALONE (every market's own top `per_market`, before any spillover)
# already reaches `cap` -- a 5th market (Riyadh, added after the original
# spec's 3x4=12 arithmetic was written) means that can now happen on its
# own, with 15 candidates each fully inside their own market's top 3. There
# is no unused capacity anywhere in that case, so it truncates to the global
# top `cap` by rank and spillover never runs -- the only situation where no
# market can exceed `per_market`.
def select_sendable(reviewed, *, per_market=3, cap=12):
    """Which reviewed jobs actually get sent, from the agent's verdicts."""
    sendable = [
        job for job in reviewed
        if job.get("ai_verdict") == "send"
        and job.get("ai_sponsorship") in ("offered", "implied")
    ]

    by_market = {}
    for job in sendable:
        by_market.setdefault(job["market"], []).append(job)

    floor, leftover = [], []
    for jobs in by_market.values():
        jobs = sorted(jobs, key=lambda j: j["ai_rank"])
        floor.extend(jobs[:per_market])
        leftover.extend(jobs[per_market:])

    floor.sort(key=lambda j: j["ai_rank"])
    if len(floor) >= cap:
        return floor[:cap]

    leftover.sort(key=lambda j: j["ai_rank"])
    selected = floor
    for job in leftover:
        if len(selected) >= cap:
            break
        selected.append(job)
    selected.sort(key=lambda j: j["ai_rank"])
    return selected
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_select_sendable.py -v`
Expected: PASS, 13 tests (3 `market_region` + 10 `select_sendable`)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, 344 -> 357 (+13), no regressions

- [ ] **Step 6: Commit**

```bash
git add job_scoring.py tests/test_select_sendable.py
git commit -m "Add market_region and select_sendable, stage 2's mechanical half

market_region classifies a location into one of his five markets by
city, not country -- market_country groups Dubai and Abu Dhabi together
and would have silently merged two markets for selection purposes.

select_sendable: 3 per market is a floor, not a ceiling -- confirmed
directly with him, a thin market's unused capacity flows to whichever
other market has more good candidates, up to a global cap of 12. The
one exception: a 5th market added after the original 3x4=12 arithmetic
means every market's own top 3 can now sum past 12 on their own, with
no unused capacity anywhere to spill -- that case truncates to the
global top 12 by rank instead, the only situation where no market can
exceed 3."
```

---

### Task 2: Persistence — DB schema and `record_review`

**Files:**
- Modify: `scraper.py`
- Test: `tests/test_review_recording.py` (new file, following
  `tests/test_application_tracking.py`'s in-memory-sqlite pattern)

**Interfaces:**
- Consumes: `job_scoring.market_region` (Task 1). This is the only
  dependency on Task 1 — otherwise independent, and Task 1's own tests do
  not depend on anything here.
- Produces: `record_review(conn, verdicts: list[dict]) -> list[dict]`. Each
  input dict has keys `job_id`, `verdict`, `reason`, `sponsorship`, and
  `rank` (present only when `verdict == "send"`; ignored otherwise). Returns
  the freshly-read `send`-verdict rows that were actually written, each
  carrying a `market` key (via `job_scoring.market_region(row["location"])`
  — **not** `market_country`, see Task 1) and its written `ai_rank`, ready
  to hand to `select_sendable`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_recording.py
import sqlite3

import scraper

CONFIG_BACKUP = dict(scraper.CONFIG)


def make_conn(rows):
    """An in-memory jobs table seeded with the given (id, overrides) rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            score INTEGER,
            notified INTEGER DEFAULT 0,
            status TEXT DEFAULT 'new',
            ai_verdict TEXT DEFAULT '',
            ai_verdict_reason TEXT DEFAULT '',
            ai_sponsorship TEXT DEFAULT '',
            ai_rank INTEGER
        )
        """
    )
    for job_id, over in rows:
        base = {
            "id": job_id, "title": "Backend Architect", "company": "Acme",
            "location": "Dubai, United Arab Emirates", "score": 60,
            "notified": 0, "status": "new",
        }
        base.update(over)
        conn.execute(
            "INSERT INTO jobs (id, title, company, location, score, notified, status) "
            "VALUES (:id, :title, :company, :location, :score, :notified, :status)",
            base,
        )
    conn.commit()
    return conn


def verdict(job_id, verdict, **over):
    base = {"job_id": job_id, "verdict": verdict, "reason": "test reason",
             "sponsorship": "implied"}
    if verdict == "send":
        base["rank"] = 1
    base.update(over)
    return base


def setup_module(module):
    scraper.CONFIG["score_threshold"] = 45


def teardown_module(module):
    scraper.CONFIG.clear()
    scraper.CONFIG.update(CONFIG_BACKUP)


def test_record_review_writes_a_send_verdict_and_returns_it():
    conn = make_conn([("j1", {})])
    written = scraper.record_review(conn, [verdict("j1", "send", rank=2)])

    row = conn.execute("SELECT * FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == "send"
    assert row["ai_verdict_reason"] == "test reason"
    assert row["ai_sponsorship"] == "implied"
    assert row["ai_rank"] == 2
    assert row["status"] == "new"
    assert len(written) == 1
    assert written[0]["market"] == "dubai"


def test_record_review_sets_status_rejected_for_a_reject_verdict():
    conn = make_conn([("j1", {})])
    scraper.record_review(conn, [verdict("j1", "reject")])

    row = conn.execute("SELECT status, ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["status"] == "rejected"
    assert row["ai_verdict"] == "reject"


def test_record_review_leaves_a_hold_verdict_as_status_new_not_notified():
    conn = make_conn([("j1", {})])
    scraper.record_review(conn, [verdict("j1", "hold")])

    row = conn.execute("SELECT status, notified, ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["status"] == "new"
    assert row["notified"] == 0
    assert row["ai_verdict"] == "hold"


def test_record_review_skips_a_job_id_not_in_todays_eligible_candidates():
    # already notified -- not an eligible candidate today
    conn = make_conn([("j1", {"notified": 1})])
    written = scraper.record_review(conn, [verdict("j1", "send")])

    row = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == ""
    assert written == []


def test_record_review_skips_an_unknown_job_id():
    conn = make_conn([])
    written = scraper.record_review(conn, [verdict("does-not-exist", "send")])
    assert written == []


def test_record_review_is_a_no_op_on_an_empty_batch():
    conn = make_conn([("j1", {})])
    written = scraper.record_review(conn, [])
    assert written == []
    row = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == ""


def test_record_review_skips_an_invalid_verdict_or_sponsorship_value():
    conn = make_conn([("j1", {}), ("j2", {})])
    written = scraper.record_review(conn, [
        verdict("j1", "definitely-maybe"),
        verdict("j2", "send", sponsorship="probably"),
    ])
    row1 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    row2 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j2'").fetchone()
    assert row1["ai_verdict"] == ""
    assert row2["ai_verdict"] == ""
    assert written == []


def test_record_review_rejects_the_whole_batch_on_a_duplicate_job_id():
    conn = make_conn([("j1", {})])
    written = scraper.record_review(conn, [
        verdict("j1", "send", rank=1),
        verdict("j1", "hold"),
    ])
    row = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    assert row["ai_verdict"] == ""
    assert written == []


def test_record_review_rejects_the_whole_batch_on_a_duplicate_rank_among_sends():
    conn = make_conn([("j1", {}), ("j2", {})])
    written = scraper.record_review(conn, [
        verdict("j1", "send", rank=1),
        verdict("j2", "send", rank=1),
    ])
    row1 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j1'").fetchone()
    row2 = conn.execute("SELECT ai_verdict FROM jobs WHERE id = 'j2'").fetchone()
    assert row1["ai_verdict"] == ""
    assert row2["ai_verdict"] == ""
    assert written == []


def test_record_review_truncates_a_reason_over_ten_words_instead_of_rejecting():
    conn = make_conn([("j1", {})])
    long_reason = " ".join(f"word{i}" for i in range(20))
    scraper.record_review(conn, [verdict("j1", "send", reason=long_reason)])

    row = conn.execute("SELECT ai_verdict_reason FROM jobs WHERE id = 'j1'").fetchone()
    assert len(row["ai_verdict_reason"].split()) == 10


def test_record_review_never_touches_score():
    conn = make_conn([("j1", {"score": 77})])
    scraper.record_review(conn, [verdict("j1", "send")])
    row = conn.execute("SELECT score FROM jobs WHERE id = 'j1'").fetchone()
    assert row["score"] == 77
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_review_recording.py -v`
Expected: FAIL with `AttributeError: module 'scraper' has no attribute 'record_review'`

- [ ] **Step 3: Add the schema migration**

In `scraper.py`'s `init_db`, add to the `CREATE TABLE IF NOT EXISTS jobs`
column list (for a fresh database) AND to the migration `for ddl in [...]`
list (for an existing one — both are needed, matching how `credibility_notes`
etc. were added):

```python
            ai_verdict TEXT DEFAULT '',
            ai_verdict_reason TEXT DEFAULT '',
            ai_sponsorship TEXT DEFAULT '',
            ai_rank INTEGER
```

```python
        "ALTER TABLE jobs ADD COLUMN ai_verdict TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN ai_verdict_reason TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN ai_sponsorship TEXT DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN ai_rank INTEGER",
```

- [ ] **Step 4: Write `record_review`**

Add to `scraper.py`, near `mark_notified`:

```python
_AI_VERDICTS = ("send", "hold", "reject")
_AI_SPONSORSHIP = ("offered", "implied", "doubtful", "excluded")


def record_review(conn, verdicts):
    """Persist the agent's verdicts, validated against today's real candidates.

    Re-queries eligible candidates itself rather than trusting the batch of
    ids it's handed -- an id that isn't notified=0/status='new'/score above
    threshold today is not applied. Writes only the four ai_* columns; never
    score or score_breakdown. Returns the written send-verdict rows, each
    carrying the market select_sendable needs.
    """
    threshold = CONFIG["score_threshold"]
    eligible_rows = conn.execute(
        "SELECT id, location FROM jobs WHERE notified = 0 AND status = 'new' AND score >= ?",
        (threshold,),
    ).fetchall()
    eligible = {row["id"]: row["location"] for row in eligible_rows}

    seen_ids = set()
    seen_ranks = set()
    for entry in verdicts:
        job_id = entry.get("job_id")
        if job_id in seen_ids:
            return []
        seen_ids.add(job_id)
        if entry.get("verdict") == "send":
            rank = entry.get("rank")
            if rank in seen_ranks:
                return []
            seen_ranks.add(rank)

    written = []
    for entry in verdicts:
        job_id = entry.get("job_id")
        verdict = entry.get("verdict")
        sponsorship = entry.get("sponsorship")
        if job_id not in eligible:
            continue
        if verdict not in _AI_VERDICTS or sponsorship not in _AI_SPONSORSHIP:
            continue

        reason = " ".join(str(entry.get("reason") or "").split()[:10])
        status_update = ", status = 'rejected'" if verdict == "reject" else ""
        rank = entry.get("rank") if verdict == "send" else None
        conn.execute(
            f"UPDATE jobs SET ai_verdict = ?, ai_verdict_reason = ?, "
            f"ai_sponsorship = ?, ai_rank = ?{status_update} WHERE id = ?",
            (verdict, reason, sponsorship, rank, job_id),
        )
        if verdict == "send":
            written.append({
                "id": job_id,
                "market": job_scoring.market_region(eligible[job_id]),
                "ai_verdict": verdict,
                "ai_verdict_reason": reason,
                "ai_sponsorship": sponsorship,
                "ai_rank": rank,
            })
    conn.commit()
    return written
```

`scraper.py:25` already has `import job_scoring` (Task 7 wired
`CONFIG["score_threshold"]` to `job_scoring.SEND_CUTOFF`) — no new import
needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_review_recording.py -v`
Expected: PASS, 11 tests

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, 357 -> 368 (+11 from this task, on top of Task 1's +13), no regressions

- [ ] **Step 7: Commit**

```bash
git add scraper.py tests/test_review_recording.py
git commit -m "Persist the AI review's verdicts: record_review + schema

Today the agent's judgment is entirely transient -- nothing survives
past the Telegram send, so a held job has no memory tomorrow and
nothing is auditable. Four new columns (ai_verdict, ai_verdict_reason,
ai_sponsorship, ai_rank) plus a 'rejected' status value. record_review
re-verifies every job_id against today's real eligible candidates
rather than trusting the batch it's handed, and only ever writes the
four new columns -- score and score_breakdown are untouchable by
construction."
```

---

### Task 3: Document the new contract in the skill file

**Files:**
- Modify: `job-hunter.skill.md`

**Interfaces:**
- Consumes: `select_sendable` (Task 1) and `record_review` (Task 2) by name
  and behavior, to describe accurately. No code interface of its own.

- [ ] **Step 1: Rewrite the Stage 2 workflow description**

In `job-hunter.skill.md`, replace the "When triggered by Hermes cron
(scheduled)" section's steps 4-5 (currently: "Hermes cron reviews unnotified
candidates with an LLM... it rejects low-seniority/student/intern/junior
roles... Hermes sends at most the best 5 human-approved recommendations back
to Telegram") with an accurate description of the new contract:

```markdown
4. Hermes cron reviews unnotified candidates with an LLM against the local
   candidate profile, feedback-adjusted score, and `feedback_learning_notes`.
   For each one it judges: whether the description reads as a real backend
   architecture/tech-lead role or a title dressed as one, whether the company
   looks real, and the sponsorship read (offered/implied/doubtful/excluded --
   required on every job regardless of verdict, since sponsorship is his one
   hard deal-breaker and a market-and-context judgment the description alone
   answers, not a phrase match). It returns a JSON array of
   `{job_id, verdict, reason, sponsorship, rank}` on stdin to
   `~/.hermes/scripts/jobhunter_review.py`, which persists every field,
   computes which `send` verdicts actually get sent (top 3 per market,
   spilling unused slots to other markets up to a global cap of 12 --
   `job_scoring.select_sendable`), and sends exactly those.
5. Jobs not selected this round are not discarded: a `hold` verdict, or a
   `send` verdict that loses the cap, both leave the job `status='new'`,
   `notified=0` so it re-competes against new arrivals tomorrow. Only an
   explicit `reject` verdict sets `status='rejected'`, which is what removes
   it from future nightly candidate batches.
```

Also update the "## Overview" line's stale
`then a Hermes cron job reviews them with an LLM before suggesting offers`
if it undersells the structured contract, and the "## Scoring System" /
"## Filters" sections only if they still describe the pre-Task-10 rubric
inaccurately (check against `job_scoring.py` at HEAD — Task 10's
title-seniority knockout and the five-dimension rubric should already be
reflected there from earlier work; fix only what's actually stale, don't
rewrite what's already accurate).

- [ ] **Step 2: Verify against the spec**

Read `docs/superpowers/specs/2026-09-04-stage-2-ai-review-design.md` once
more after writing, and confirm every claim in the new skill.md text matches
it exactly: the JSON field names, the enum values, the per-market/spillover/
cap numbers, and the `status='rejected'` vs `status='new'` distinction.

- [ ] **Step 3: Commit**

```bash
git add job-hunter.skill.md
git commit -m "Document the Stage 2 AI review's actual contract in the skill

Replaces the vague 'reviews with an LLM... keeps the best 5' description
with the real JSON contract now that Tasks 1-2 implement it."
```

---

## What this plan does not cover

`jobhunter_review.py`, the Hermes script that calls `record_review` and
`select_sendable` from the Pi side, and the Hermes cron job's `prompt` field
itself. Both live outside this repo (`~/.hermes/scripts/`,
`~/.hermes/cron/jobs.json`, backed up into the RaspberryPi repo at
`config/home/ala/.hermes/`) and are a manual rollout step once these three
tasks are reviewed — see the spec's "Rollout" section. The collector's
`MAX_CANDIDATES` (25, spec wants 40) lives in the same outside-this-repo
location and is fixed in that same rollout step.

The digest message format, `show:`/`more:` callbacks, and any other
Telegram UI change — a separate design pass per the spec's "Out of scope".
`feedback_adjusted_score` / `apply_feedback_learning` — pre-existing,
untouched. The stack_fit weight refit (Task 9 of the scoring-engine plan) —
still blocked on the user's 60 ratings, unrelated to this work.
