# Digest Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-job Telegram blast with one consolidated nightly
digest, using a text-reply drill-down model since the original button-based
design cannot work (see spec).

**Architecture:** Two new pure/DB functions in `scraper.py`
(`format_digest_message`, `send_digest`), one new CLI flag
(`--list-queued`) for the on-demand "more" flow, and a `job-hunter.skill.md`
update describing how a numeric reply resolves to a job. Wiring
`jobhunter_review.py` (from the Stage 2 plan) to call `send_digest` instead
of `notify_new_jobs`/`mark_notified` is a manual rollout step outside this
repo, same as Stage 2's own rollout — **not** part of this plan's task list.

**Tech Stack:** Python 3.13, stdlib only, sqlite3. pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-04-digest-delivery-design.md`

## Global Constraints

- `format_digest_message(sent, queued_count, queued_top_scores, *,
  today=None)` in `scraper.py`. Pure, no DB access. `sent` arrives in any
  order; the function groups by market, sorts each market's jobs by
  `ai_rank`, then assigns fresh sequential display numbers 1..N in that
  final order — never a copy of `ai_rank` itself.
- `DIGEST_MARKET_ORDER = ("dubai", "abu dhabi", "jeddah", "riyadh",
  "switzerland")` — lowercase, matching `job_scoring.market_region`'s
  return values (not `CONFIG["regions"]`'s capitalized keys).
- Hiring route: `job_scoring.employer_fit(job) == job_scoring.DIRECT_EMPLOYER`
  → "hires directly", else "via a recruiter". Sponsorship: displayed
  directly from `job["ai_sponsorship"]`.
- `send_digest(token, chat_id, conn, selected)` in `scraper.py`. `selected`
  is a list of full job row dicts (plain dicts or `sqlite3.Row`, handled
  either way via `dict(row)`). Sends exactly one Telegram message, then
  marks exactly the selected jobs notified via the existing
  `mark_notified`. The queued count/scores query explicitly excludes the
  selected ids (`NOT IN`), not by relying on `mark_notified` having already
  run — order-independent by construction.
- `list_queued_jobs(conn, limit=10)` in `scraper.py`, exposed via
  `--list-queued` (and `--limit N`, default 10) on the existing argparse
  CLI, mirroring `--get-job`'s existing shape.
- Every existing test must still pass: `.venv/bin/python -m pytest -q tests`
  currently reports 369 passed (baseline after the Stage 2 plan).

---

### Task 1: `format_digest_message`

**Files:**
- Modify: `scraper.py`
- Test: `tests/test_digest.py` (new file — distinct concern from
  `test_format.py`'s per-job card tests)

**Interfaces:**
- Consumes: `job_scoring.employer_fit`, `job_scoring.DIRECT_EMPLOYER`,
  `job_scoring.market_region` (values only, not the function itself — this
  task receives jobs that already carry a `market` key), `scraper.job_age`
  (already exists).
- Produces: `DIGEST_MARKET_ORDER: tuple[str, ...]`,
  `format_digest_message(sent, queued_count, queued_top_scores, *,
  today=None) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_digest.py
from datetime import datetime, timezone

import scraper


def job(**over):
    base = {
        "id": "j1", "title": "Backend Lead", "company": "Acme",
        "score": 80, "market": "dubai", "ai_rank": 1,
        "ai_sponsorship": "implied", "ai_verdict_reason": "solid fit",
        "tech_required": "Java, Spring", "date_posted": "", "location": "Dubai",
        "recruiter_company": "", "credibility_notes": "",
    }
    base.update(over)
    return base


def test_format_digest_message_shows_every_market_when_empty():
    msg = scraper.format_digest_message([], 0, [])
    assert msg.count("nothing today") == 5
    assert "0 sent" in msg


def test_format_digest_message_numbers_by_display_order_not_ai_rank():
    # A Dubai job with a WORSE (higher) ai_rank than a Switzerland job must
    # still be numbered 1, because Dubai prints first in DIGEST_MARKET_ORDER.
    jobs = [
        job(id="ch1", market="switzerland", ai_rank=1, title="CH Job"),
        job(id="dx1", market="dubai", ai_rank=5, title="Dubai Job"),
    ]
    msg = scraper.format_digest_message(jobs, 0, [])
    lines = msg.splitlines()
    dubai_idx = next(i for i, l in enumerate(lines) if "Dubai Job" in l)
    ch_idx = next(i for i, l in enumerate(lines) if "CH Job" in l)
    assert lines[dubai_idx].startswith("1️⃣")
    assert lines[ch_idx].startswith("2️⃣")
    assert dubai_idx < ch_idx


def test_format_digest_message_shows_hiring_route_for_both_employer_tiers():
    direct_job = job(id="d1", company="Acme")
    agency_job = job(id="a1", company="Confidential Recruitment Agency")
    assert "hires directly" in scraper.format_digest_message([direct_job], 0, [])
    assert "via a recruiter" in scraper.format_digest_message([agency_job], 0, [])


def test_format_digest_message_shows_sponsorship_read():
    msg = scraper.format_digest_message([job(ai_sponsorship="offered")], 0, [])
    assert "sponsorship offered" in msg


def test_format_digest_message_shows_the_queued_line():
    msg = scraper.format_digest_message([], 6, [71, 68, 66])
    assert "6 more queued" in msg
    assert "71, 68, 66" in msg


def test_format_digest_message_omits_queued_line_when_nothing_queued():
    msg = scraper.format_digest_message([], 0, [])
    assert "more queued" not in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_digest.py -v`
Expected: FAIL with `AttributeError: module 'scraper' has no attribute 'format_digest_message'`

- [ ] **Step 3: Write the implementation**

Add to `scraper.py`, near `format_job_message`:

```python
# The nightly digest's fixed market order and header labels. Lowercase to
# match job_scoring.market_region's return values directly -- NOT
# CONFIG["regions"]'s capitalized keys, a different casing convention for
# a different purpose (scrape-time region search vs. display grouping).
DIGEST_MARKET_ORDER = ("dubai", "abu dhabi", "jeddah", "riyadh", "switzerland")
DIGEST_MARKET_LABELS = {
    "dubai": "\U0001f1e6\U0001f1ea DUBAI",
    "abu dhabi": "\U0001f1e6\U0001f1ea ABU DHABI",
    "jeddah": "\U0001f1f8\U0001f1e6 JEDDAH",
    "riyadh": "\U0001f1f8\U0001f1e6 RIYADH",
    "switzerland": "\U0001f1e8\U0001f1ed SWITZERLAND",
}
_DIGEST_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣",
                   "5️⃣", "6️⃣", "7️⃣", "8️⃣",
                   "9️⃣", "\U0001f51f")


def _digest_number(n):
    """The nth entry's label -- a keycap emoji up to 10, a plain number after."""
    return _DIGEST_NUMBERS[n - 1] if 1 <= n <= len(_DIGEST_NUMBERS) else f"{n}."


def format_digest_message(sent, queued_count, queued_top_scores, *, today=None):
    """The one nightly message: sent jobs grouped by market, then the queue.

    `sent` is unordered on input -- this function groups by market, sorts
    each market's jobs by ai_rank, then numbers them 1..N in that final
    display order. The number is a fresh sequential label, not ai_rank
    itself: ai_rank has gaps (candidates that lost the cap) and doesn't
    respect market grouping, and a reply of "2" has to mean "the second
    job as printed," not "whatever ai_rank happens to be 2."
    """
    today = today or datetime.now(timezone.utc)
    by_market = {}
    for job in sent:
        by_market.setdefault(job["market"], []).append(job)
    for jobs in by_market.values():
        jobs.sort(key=lambda j: j["ai_rank"])

    lines = [
        f"\U0001f3af {today.strftime('%-d %b')} · {len(sent)} sent · {queued_count} queued",
        "",
    ]

    number = 1
    for market in DIGEST_MARKET_ORDER:
        jobs = by_market.get(market, [])
        label = DIGEST_MARKET_LABELS[market]
        if not jobs:
            lines.append(f"{label} — nothing today")
            continue
        lines.append(label)
        for job in jobs:
            direct = job_scoring.employer_fit(job) == job_scoring.DIRECT_EMPLOYER
            hiring_route = (
                "\U0001f91d hires directly" if direct else "\U0001f575 via a recruiter"
            )
            sponsorship = job.get("ai_sponsorship", "")
            lines.append(f"{_digest_number(number)} {job['title']}")
            lines.append(
                f"   ⭐ {job['score']} · \U0001f3e2 {job['company']} · "
                f"{hiring_route} · \U0001f6c2 sponsorship {sponsorship}"
            )
            req = job.get("tech_required") or ""
            age = job_age(job.get("date_posted", ""))
            line2 = []
            if req:
                line2.append(f"\U0001f9e9 {req}")
            if age:
                line2.append(f"\U0001f5d3 {age}")
            if line2:
                lines.append("   " + " · ".join(line2))
            reason = job.get("ai_verdict_reason") or ""
            if reason:
                lines.append(f"   \U0001f4ac {reason}")
            number += 1
        lines.append("")

    if queued_count:
        top = ", ".join(str(s) for s in queued_top_scores)
        lines.append(f"↷ {queued_count} more queued (⭐ {top})")
        lines.append("")

    lines.append(
        'Reply with a number to see the full listing, or "more" to see what\'s queued.'
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_digest.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, 369 -> 375 (+6), no regressions

- [ ] **Step 6: Commit**

```bash
git add scraper.py tests/test_digest.py
git commit -m "Add format_digest_message: one message instead of per-job cards

Groups sent jobs by market (fixed order), sorts each market's jobs by
ai_rank, then numbers them 1..N in that final display order -- not a
copy of ai_rank, which has gaps and doesn't respect market grouping.
Every market gets a header line even when empty, per the original
digest spec's requirement that a quiet market be distinguishable from
a failed run."
```

---

### Task 2: `send_digest` and the queued-jobs query

**Files:**
- Modify: `scraper.py`
- Test: `tests/test_digest.py` (same file as Task 1)

**Interfaces:**
- Consumes: `format_digest_message` (Task 1), `job_scoring.market_region`,
  `scraper.send_telegram`, `scraper.mark_notified` (both already exist).
- Produces: `send_digest(token, chat_id, conn, selected) -> None`.

- [ ] **Step 1: Write the failing tests**

Add `import sqlite3` and `from unittest import mock` to the top of
`tests/test_digest.py` (beside the existing `import scraper`), then append
the rest below:

```python
CONFIG_BACKUP = dict(scraper.CONFIG)


def make_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            score INTEGER, notified INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
            date_posted TEXT DEFAULT '', tech_required TEXT DEFAULT '',
            recruiter_company TEXT DEFAULT '', credibility_notes TEXT DEFAULT '',
            ai_verdict TEXT DEFAULT '', ai_verdict_reason TEXT DEFAULT '',
            ai_sponsorship TEXT DEFAULT '', ai_rank INTEGER
        )
        """
    )
    for job_id, over in rows:
        base = {"id": job_id, "title": "Backend Architect", "company": "Acme",
                "location": "Dubai, United Arab Emirates", "score": 60,
                "notified": 0, "status": "new", "ai_rank": None}
        base.update(over)
        conn.execute(
            "INSERT INTO jobs (id, title, company, location, score, notified, status, ai_rank) "
            "VALUES (:id, :title, :company, :location, :score, :notified, :status, :ai_rank)",
            base,
        )
    conn.commit()
    return conn


def setup_module(module):
    scraper.CONFIG["score_threshold"] = 45


def teardown_module(module):
    scraper.CONFIG.clear()
    scraper.CONFIG.update(CONFIG_BACKUP)


def test_send_digest_sends_exactly_one_message():
    conn = make_conn([("sent1", {"score": 80, "ai_rank": 1}), ("sent2", {"score": 70, "ai_rank": 2})])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
                for jid in ("sent1", "sent2")]
    with mock.patch.object(scraper, "send_telegram") as fake_send:
        fake_send.return_value = True
        scraper.send_digest("tok", "chat", conn, selected)
    assert fake_send.call_count == 1


def test_send_digest_marks_only_the_selected_jobs_notified():
    conn = make_conn([
        ("sent1", {"score": 80, "ai_rank": 1}),
        ("queued1", {"score": 60}),
    ])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id = ?", ("sent1",)).fetchone())]
    with mock.patch.object(scraper, "send_telegram") as fake_send:
        fake_send.return_value = True
        scraper.send_digest("tok", "chat", conn, selected)
    sent_row = conn.execute("SELECT notified FROM jobs WHERE id='sent1'").fetchone()
    queued_row = conn.execute("SELECT notified FROM jobs WHERE id='queued1'").fetchone()
    assert sent_row["notified"] == 1
    assert queued_row["notified"] == 0


def test_send_digest_queued_count_excludes_the_selected_jobs_and_ineligible_ones():
    conn = make_conn([
        ("sent1", {"score": 80, "ai_rank": 1}),
        ("queued1", {"score": 60}),
        ("queued2", {"score": 50}),
        ("below_threshold", {"score": 20}),
        ("already_notified", {"score": 90, "notified": 1}),
    ])
    selected = [dict(conn.execute("SELECT * FROM jobs WHERE id = ?", ("sent1",)).fetchone())]
    with mock.patch.object(scraper, "format_digest_message") as fake_format:
        fake_format.return_value = "digest text"
        with mock.patch.object(scraper, "send_telegram") as fake_send:
            fake_send.return_value = True
            scraper.send_digest("tok", "chat", conn, selected)
    _, queued_count, queued_top_scores = fake_format.call_args[0]
    assert queued_count == 2
    assert queued_top_scores == [60, 50]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_digest.py -v`
Expected: the 3 new tests FAIL with `AttributeError: module 'scraper' has no attribute 'send_digest'`

- [ ] **Step 3: Write the implementation**

Add to `scraper.py`, near `notify_new_jobs`:

```python
def _queued_after_send(conn, sent_ids):
    """Scores of eligible jobs not selected tonight -- what's still queued.

    Excludes the just-selected ids explicitly rather than relying on
    mark_notified having already run first -- order-independent by
    construction, so a future reordering of send_digest's steps can't
    silently double-count the jobs it just sent.
    """
    threshold = CONFIG["score_threshold"]
    query = "SELECT score FROM jobs WHERE notified = 0 AND status = 'new' AND score >= ?"
    params = [threshold]
    if sent_ids:
        placeholders = ",".join("?" * len(sent_ids))
        query += f" AND id NOT IN ({placeholders})"
        params.extend(sent_ids)
    query += " ORDER BY score DESC"
    return [row[0] for row in conn.execute(query, params).fetchall()]


def send_digest(token, chat_id, conn, selected):
    """Compose and send the one nightly digest, then mark selected jobs notified."""
    sent = []
    for row in selected:
        job = dict(row)
        job["market"] = job_scoring.market_region(job.get("location"))
        sent.append(job)

    sent_ids = [job["id"] for job in sent]
    queued_scores = _queued_after_send(conn, sent_ids)
    queued_count = len(queued_scores)
    queued_top_scores = queued_scores[:3]

    message = format_digest_message(sent, queued_count, queued_top_scores)
    send_telegram(token, chat_id, message)
    if sent_ids:
        mark_notified(conn, sent_ids)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_digest.py -v`
Expected: PASS, 9 tests (6 from Task 1 + 3 from this task)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, 375 -> 378 (+3), no regressions

- [ ] **Step 6: Commit**

```bash
git add scraper.py tests/test_digest.py
git commit -m "Add send_digest: one Telegram send instead of a per-job loop

The queued count excludes the just-selected ids explicitly (NOT IN),
not by relying on mark_notified having already run -- order-independent
by construction."
```

---

### Task 3: `--list-queued` CLI flag

**Files:**
- Modify: `scraper.py`
- Test: `tests/test_digest.py` (same file)

**Interfaces:**
- Consumes: `job_scoring.market_region`.
- Produces: `list_queued_jobs(conn, limit=10) -> list[dict]`, plus the
  `--list-queued` / `--limit` CLI flags.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_digest.py`:

```python
def test_list_queued_jobs_respects_limit_and_excludes_notified():
    conn = make_conn([
        ("q1", {"score": 90}),
        ("q2", {"score": 80}),
        ("q3", {"score": 70}),
        ("notified_already", {"score": 95, "notified": 1}),
    ])
    result = scraper.list_queued_jobs(conn, limit=2)
    assert [r["score"] for r in result] == [90, 80]


def test_list_queued_jobs_includes_market():
    conn = make_conn([("q1", {"score": 90, "location": "Zurich, Switzerland"})])
    result = scraper.list_queued_jobs(conn, limit=10)
    assert result[0]["market"] == "switzerland"


def test_list_queued_jobs_empty_queue_returns_empty_list():
    conn = make_conn([("below_threshold", {"score": 20})])
    assert scraper.list_queued_jobs(conn) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_digest.py -v`
Expected: the 3 new tests FAIL with `AttributeError: module 'scraper' has no attribute 'list_queued_jobs'`

- [ ] **Step 3: Write the implementation**

Add to `scraper.py`, near `get_job_by_id`:

```python
def list_queued_jobs(conn, limit=10):
    """Top eligible-but-unsent jobs by score, for the 'more' conversational flow."""
    threshold = CONFIG["score_threshold"]
    rows = conn.execute(
        "SELECT id, title, company, location, score FROM jobs "
        "WHERE notified = 0 AND status = 'new' AND score >= ? "
        "ORDER BY score DESC LIMIT ?",
        (threshold, limit),
    ).fetchall()
    return [
        {
            "id": row[0], "title": row[1], "company": row[2],
            "market": job_scoring.market_region(row[3]), "score": row[4],
        }
        for row in rows
    ]
```

In `parse_args()`, beside the `--get-job` line:

```python
    parser.add_argument("--list-queued", action="store_true", help="Print top queued jobs as JSON")
    parser.add_argument("--limit", type=int, default=10, metavar="N", help="Row limit for --list-queued")
```

In `main()`, beside the `args.get_job` block:

```python
    if args.list_queued:
        conn = init_db()
        jobs = list_queued_jobs(conn, limit=args.limit)
        conn.close()
        print(json.dumps(jobs, indent=2))
        return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_digest.py -v`
Expected: PASS, 12 tests

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, 378 -> 381 (+3), no regressions

- [ ] **Step 6: Smoke-test the CLI directly**

Run: `.venv/bin/python scraper.py --list-queued --limit 3`
Expected: valid JSON array printed to stdout (empty array `[]` is fine if
the real `data/jobs.db` has nothing queued right now — that is itself a
correct result, not a failure).

- [ ] **Step 7: Commit**

```bash
git add scraper.py tests/test_digest.py
git commit -m "Add --list-queued: the on-demand half of the 'more' reply

Mirrors --get-job's existing shape -- a plain query the agent invokes
conversationally, not a scheduled script."
```

---

### Task 4: Document the digest and reply model in the skill file

**Files:**
- Modify: `job-hunter.skill.md`

**Interfaces:**
- Consumes: `format_digest_message`, `send_digest`, `list_queued_jobs`
  (Tasks 1-3) by name and behavior, to describe accurately. No code
  interface of its own.

- [ ] **Step 1: Rewrite the delivery description**

In `job-hunter.skill.md`, replace whatever the Stage 2 rollout's Task 3
wrote about sending "the existing per-job Telegram card" with:

```markdown
6. The reviewed jobs Hermes selects for sending (via
   `job_scoring.select_sendable`) go out as ONE nightly digest message,
   not one message per job — grouped by market, numbered in the order
   printed (not by `ai_rank`, which has gaps and doesn't respect market
   grouping). A market with nothing to send still gets a line, so a quiet
   market reads as quiet, not broken.
7. A bare numeric reply that follows the digest (e.g. "2") refers to that
   job — resolved from your own memory of the digest you just sent, not
   from any stored mapping. Look up that job's id, run
   `scraper.py --get-job <id>`, and continue into the existing tailoring
   workflow exactly as if the user had named the job directly.
8. A reply of "more" runs `scraper.py --list-queued` and presents the
   result as a short follow-up text list — not a second digest, not
   numbered for further drill-down.
9. The existing "interested"/"skip" triggers are unchanged: once a
   specific job is in view (from a numbered reply or otherwise), those
   words work exactly as documented above.
```

Renumber the surrounding steps if the section they're inserted into uses
sequential numbering (check the actual current numbering at the insertion
point — don't assume steps 6-9 are the right numbers without checking).

- [ ] **Step 2: Verify against the spec**

Read `docs/superpowers/specs/2026-09-04-digest-delivery-design.md` once
more after writing, and confirm every claim matches: the numbering rule,
the market order, the icon meanings, and that nothing describes reviving
`callback_handler.py` or button-based interaction (explicitly out of
scope).

- [ ] **Step 3: Commit**

```bash
git add job-hunter.skill.md
git commit -m "Document the digest and text-reply drill-down in the skill

Buttons don't work in production (traced Hermes's own Telegram adapter:
it only recognizes its own internal callback prefixes, and
callback_handler.py isn't running). This documents the text-reply model
that uses the same path 'interested' already works through today."
```

---

## What this plan does not cover

Wiring `jobhunter_review.py` to call `send_digest` instead of
`notify_new_jobs`/`mark_notified` — that script lives outside this repo
(`~/.hermes/scripts/`, backed up at
`config/home/ala/.hermes/scripts/jobhunter_review.py` in the RaspberryPi
repo) and is a manual rollout step once these four tasks are reviewed,
same as the Stage 2 plan's own rollout.

Reviving `callback_handler.py`, extending Hermes's Telegram adapter, or any
other button-based interaction path — abandoned per the spec, not deferred.
