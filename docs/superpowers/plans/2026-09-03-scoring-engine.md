# Scoring Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the keyword-count job scorer with knockouts plus five weighted
dimensions on a 0-100 scale, and prove it beats the measured baseline of AUC
0.565 on the labelled jobs.

**Architecture:** A new pure module `job_scoring.py` holds every scoring
decision and imports nothing from `scraper.py`, so it is testable without a
database or network. `scraper.py` calls it. An evaluation script measures any
scorer against labelled jobs, so "better" is a number, not an opinion.

**Tech Stack:** Python 3.13, stdlib only. pytest for tests. Existing sqlite3
database at `data/jobs.db`.

**Spec:** `docs/superpowers/specs/2026-09-03-job-scoring-design.md`

## Global Constraints

- Weights: stack fit 35, role fit 30, seniority 15, employer 12, freshness 8. Sum is 100.
- Bands: excellent >= 75, good 60-74, normal 45-59, below 45 is never sent.
- Stage-1 cutoff: 45. Stage-2 input cap: 40 jobs.
- Knockouts are boolean and run before any arithmetic. A knockout returns a reason string.
- `job_scoring.py` must not import `scraper.py`. Dependency runs one way only.
- Sponsorship is NOT a stage-1 knockout. The only sponsorship rule here is the existing explicit-refusal phrase list.
- The four user-specified excluded titles stay literal substrings: `staff engineer`, `senior architect`, `senior cloud architect`, `senior lead software engineer`.
- Every existing test must still pass: `.venv/bin/python -m pytest -q tests` currently reports 183 passed.
- Baseline to beat: AUC 0.565, with 2 of 10 interested jobs above threshold.

---

### Task 1: Title normalisation and blocked titles

**Files:**
- Create: `job_scoring.py`
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SENIORITY_WORDS: tuple[str, ...]`, `normalise_title(title: str) -> str`,
  `ROLE_FAMILY_BLOCKS: tuple[str, ...]`, `LITERAL_BLOCKS: tuple[str, ...]`,
  `blocked_title(job: dict) -> str | None` returning a reason or None.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_job_scoring.py
import job_scoring


def test_normalise_title_strips_seniority_words():
    assert job_scoring.normalise_title("Senior DevOps Manager") == "devops"
    assert job_scoring.normalise_title("Lead DevOps Engineer") == "devops engineer"
    assert job_scoring.normalise_title("Head of QA") == "qa"


def test_blocked_title_catches_every_devops_wording():
    for title in ("DevOps Manager", "Lead DevOps", "Senior DevOps Engineer", "DevOps"):
        assert job_scoring.blocked_title({"title": title}), title


def test_blocked_title_keeps_the_titles_he_wants():
    for title in ("Software Architect", "Cloud Architect", "Tech Lead",
                  "Senior Backend Engineer", "Lead Software Engineer",
                  "Senior Software Architect", "Solutions Architect"):
        assert job_scoring.blocked_title({"title": title}) is None, title


def test_blocked_title_honours_the_four_literal_blocks():
    for title in ("Staff Engineer", "Senior Architect", "Senior Cloud Architect",
                  "Senior Lead Software Engineer"):
        assert job_scoring.blocked_title({"title": title}), title
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_scoring'`

- [ ] **Step 3: Write minimal implementation**

```python
# job_scoring.py
"""Scoring for job postings: knockouts first, then five weighted dimensions.

Pure functions over plain dicts shaped like rows of the `jobs` table. This
module imports nothing from scraper.py so it can be tested without a database.
"""

import re
from datetime import datetime, timezone

# Words that describe how senior a role is, not what the role is. Stripping
# them collapses "Senior DevOps Manager" and "Lead DevOps" onto one family, so
# a blocked family cannot be smuggled past the filter by a new prefix.
SENIORITY_WORDS = (
    "senior", "junior", "lead", "principal", "staff", "chief", "head of",
    "head", "manager", "director", "vp", "associate", "assistant", "mid",
    "mid-level", "sr", "jr", "of",
)

# Role families he never wants, matched after normalisation.
ROLE_FAMILY_BLOCKS = (
    "devops", "sre", "site reliability", "qa", "quality assurance", "sdet",
    "test engineer", "manual test", "network", "infrastructure", "sysadmin",
    "system administrator", "systems administrator", "systems engineer",
    "platform engineer", "data engineer", "data architect", "data platform",
    "machine learning", "ml engineer", "ml architect", "security engineer",
    "security architect", "cybersecurity", "information security", "infosec",
    "embedded", "firmware", "hardware", "frontend", "front-end", "ui engineer",
    "ui developer", "ux engineer", "react", "angular", "vue",
)

# Exact strings he asked to block. These stay literal: stripping "senior" from
# "senior cloud architect" would turn it back into a title he wants.
LITERAL_BLOCKS = (
    "staff engineer", "staff software engineer", "senior architect",
    "senior cloud architect", "senior lead software engineer",
)

_WORD = re.compile(r"[a-z0-9+#.]+")


def normalise_title(title):
    """Lowercase a title and drop the words that only describe seniority."""
    words = _WORD.findall(str(title or "").lower())
    kept = [w for w in words if w not in SENIORITY_WORDS]
    return " ".join(kept)


def blocked_title(job):
    """Return a reason string when the title is one he never wants, else None."""
    raw = str(job.get("title") or "").lower()
    for phrase in LITERAL_BLOCKS:
        if phrase in raw:
            return f"blocked title: {phrase}"

    normalised = normalise_title(raw)
    for family in ROLE_FAMILY_BLOCKS:
        if re.search(rf"\b{re.escape(family)}\b", normalised):
            return f"blocked role family: {family}"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Collapse blocked role families onto one normalised title

DevOps Manager scored 22 and reached the digest because the exclude list
held devops engineer and devops lead but not that third wording. Stripping
seniority words first means one family entry covers every prefix."
```

---

### Task 2: The remaining knockouts

**Files:**
- Modify: `job_scoring.py`
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: `blocked_title` from Task 1.
- Produces: `duplicate_key(job: dict) -> str`,
  `knockout(job: dict, *, allowed_locations: tuple[str, ...], max_experience: int = 8, seen_keys: frozenset[str] = frozenset()) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_job_scoring.py
UAE = ("dubai", "abu dhabi", "jeddah", "switzerland")


def job(**over):
    base = {
        "title": "Software Architect", "company": "Acme",
        "location": "Dubai, United Arab Emirates", "description": "Java and Spring Boot.",
        "min_experience": 6, "tech_required": "java, spring boot", "tech_nice_to_have": "",
        "date_posted": "", "company_website": "https://acme.example", "recruiter_company": "",
    }
    base.update(over)
    return base


def test_knockout_rejects_a_location_outside_the_markets():
    assert job_scoring.knockout(job(location="Cairo, Egypt"), allowed_locations=UAE)


def test_knockout_rejects_junior_titles_outright():
    assert job_scoring.knockout(job(title="Junior Software Architect"), allowed_locations=UAE)
    assert job_scoring.knockout(job(title="Graduate Software Engineer"), allowed_locations=UAE)


def test_knockout_rejects_more_experience_than_he_has():
    assert job_scoring.knockout(job(min_experience=12), allowed_locations=UAE)
    assert job_scoring.knockout(job(min_experience=8), allowed_locations=UAE) is None


def test_knockout_rejects_an_explicit_refusal_to_sponsor():
    text = "We will not sponsor visas for this role."
    assert job_scoring.knockout(job(description=text), allowed_locations=UAE)


def test_knockout_rejects_a_duplicate_of_a_job_already_seen():
    first = job(title="Senior Technical Architect", company="Inception")
    key = job_scoring.duplicate_key(first)
    assert job_scoring.knockout(first, allowed_locations=UAE, seen_keys=frozenset({key}))


def test_duplicate_key_ignores_seniority_and_case():
    a = job_scoring.duplicate_key(job(title="Senior Technical Architect", company="Inception"))
    b = job_scoring.duplicate_key(job(title="  technical   architect ", company="INCEPTION"))
    assert a == b


def test_knockout_passes_a_job_he_wants():
    assert job_scoring.knockout(job(), allowed_locations=UAE) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: FAIL with `AttributeError: module 'job_scoring' has no attribute 'duplicate_key'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to job_scoring.py

# Promoted from a -3 penalty to a knockout: as points it was defeatable by
# buzzword count, so junior roles with a dense stack list still got through.
JUNIOR_WORDS = (
    "junior", "intern", "internship", "entry level", "entry-level",
    "graduate", "fresh graduate", "trainee", "apprentice",
)

# The rare posting that says outright it will not sponsor. 2 of 4,575 postings
# in the corpus match anything like this, so it is a safety net, not the gate.
REFUSES_SPONSORSHIP = (
    "no visa sponsorship", "will not sponsor", "won't sponsor",
    "does not sponsor", "unable to sponsor", "not able to sponsor",
    "no relocation", "local candidates only", "local hires only",
)


def duplicate_key(job):
    """Identity of a posting for deduplication: normalised title plus company."""
    company = " ".join(_WORD.findall(str(job.get("company") or "").lower()))
    return f"{normalise_title(job.get('title'))}|{company}"


def knockout(job, *, allowed_locations, max_experience=8, seen_keys=frozenset()):
    """Return the reason this job is rejected outright, or None to keep it.

    Runs before any scoring. No number of matching keywords overturns one of
    these, which is the whole point of separating them from the score.
    """
    reason = blocked_title(job)
    if reason:
        return reason

    title = str(job.get("title") or "").lower()
    for word in JUNIOR_WORDS:
        if word in title:
            return f"too junior: {word}"

    location = str(job.get("location") or "").lower()
    if allowed_locations and not any(a in location for a in allowed_locations):
        return f"outside the configured markets: {job.get('location') or 'unknown'}"

    years = job.get("min_experience", -1)
    if isinstance(years, int) and years > max_experience:
        return f"wants {years}+ years, over the {max_experience} cap"

    description = str(job.get("description") or "").lower()
    for phrase in REFUSES_SPONSORSHIP:
        if phrase in description:
            return f"refuses sponsorship: {phrase}"

    if duplicate_key(job) in seen_keys:
        return "duplicate of a posting already seen"

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Make junior, duplicate, and refused sponsorship hard knockouts

Inception posted the same architect role three times in one night; as three
rows they would have taken three of the twelve daily slots."
```

---

### Task 3: Stack fit and role fit

**Files:**
- Modify: `job_scoring.py`
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: `normalise_title` from Task 1.
- Produces: `stack_fit(job: dict) -> float` and `role_fit(job: dict) -> float`, both returning 0.0-1.0.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_job_scoring.py
def test_stack_fit_rewards_his_core_stack_being_required():
    core = job(tech_required="java, kotlin, spring boot, microservices, aws")
    thin = job(tech_required="php, wordpress")
    assert job_scoring.stack_fit(core) > 0.8
    assert job_scoring.stack_fit(thin) < 0.2


def test_stack_fit_counts_nice_to_have_for_less_than_required():
    required = job(tech_required="java, spring boot", tech_nice_to_have="")
    optional = job(tech_required="", tech_nice_to_have="java, spring boot")
    assert job_scoring.stack_fit(required) > job_scoring.stack_fit(optional) > 0


def test_stack_fit_is_never_above_one():
    everything = job(tech_required="java, kotlin, spring boot, spring, microservices, "
                                   "aws, azure, terraform, kubernetes, docker, "
                                   "postgresql, mongodb, redis, kafka, rest")
    assert job_scoring.stack_fit(everything) <= 1.0


def test_role_fit_ranks_the_families_the_way_he_does():
    architect = job(title="Software Architect")
    senior_backend = job(title="Senior Backend Engineer")
    manager = job(title="Engineering Manager")
    fullstack = job(title="Full Stack Architect")
    generic = job(title="software engineer")
    assert job_scoring.role_fit(architect) == 1.0
    assert job_scoring.role_fit(senior_backend) == 0.8
    assert job_scoring.role_fit(manager) == 0.5
    assert job_scoring.role_fit(fullstack) == 0.4
    assert job_scoring.role_fit(generic) == 0.3


def test_role_fit_reads_full_stack_before_architect():
    # "Full Stack Architect" contains "architect" but half the job is frontend.
    assert job_scoring.role_fit(job(title="Full Stack Architect")) == 0.4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: FAIL with `AttributeError: module 'job_scoring' has no attribute 'stack_fit'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to job_scoring.py

# His stack in three rings. Core is what he is hired for; cloud is the platform
# he builds on; adjacent is credible but not differentiating.
CORE_STACK = ("java", "kotlin", "spring boot", "spring", "microservices")
CLOUD_STACK = ("aws", "azure", "terraform", "kubernetes", "docker")
ADJACENT_STACK = (
    "nestjs", "typescript", ".net", "c#", "postgresql", "mongodb", "redis",
    "kafka", "rabbitmq", "rest", "graphql", "grpc", "event-driven", "ddd",
)

# A technology the posting requires counts fully; one it merely likes counts
# less. The old scorer gave a flat +1 for any nice-to-have match at all.
NICE_TO_HAVE_CREDIT = 0.4

# Ordered: the first family whose pattern matches the normalised title wins, so
# "full stack architect" resolves to full-stack rather than architect.
ROLE_FAMILIES = (
    (0.4, ("full stack", "fullstack", "full-stack")),
    (1.0, ("architect", "tech lead", "technical lead", "software lead",
           "lead software engineer", "lead backend", "backend lead")),
    (0.8, ("backend engineer", "backend developer", "backend software engineer",
           "software engineer backend", "platform architect")),
    (0.5, ("engineering manager", "head of engineering", "vp engineering",
           "director of engineering", "cto")),
)
GENERIC_ROLE_FIT = 0.3


def _ring_coverage(terms, required, optional):
    """Fraction of a ring the posting asks for, nice-to-have counting for less."""
    if not terms:
        return 0.0
    total = 0.0
    for term in terms:
        if term in required:
            total += 1.0
        elif term in optional:
            total += NICE_TO_HAVE_CREDIT
    return min(1.0, total / len(terms))


def stack_fit(job):
    """How central his stack is to the posting, 0.0-1.0."""
    required = str(job.get("tech_required") or "").lower()
    optional = str(job.get("tech_nice_to_have") or "").lower()
    return min(1.0, (
        0.60 * _ring_coverage(CORE_STACK, required, optional)
        + 0.30 * _ring_coverage(CLOUD_STACK, required, optional)
        + 0.10 * _ring_coverage(ADJACENT_STACK, required, optional)
    ) / 0.62)


def role_fit(job):
    """How close the title is to the work he wants, 0.0-1.0."""
    title = normalise_title(job.get("title"))
    raw = str(job.get("title") or "").lower()
    for value, phrases in ROLE_FAMILIES:
        for phrase in phrases:
            if phrase in title or phrase in raw:
                return value
    return GENERIC_ROLE_FIT
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: PASS, 16 tests. If `test_stack_fit_rewards_his_core_stack_being_required`
fails on the upper bound, adjust the `/ 0.62` normaliser and rerun — the divisor
exists so a posting requiring his whole core plus most of the cloud ring reaches
1.0 rather than topping out around 0.6.

- [ ] **Step 5: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Score stack centrality and role family instead of counting words

The old scorer read 'aws' three times as a better signal than 'Kotlin,
Spring Boot' once, which is how software engineer @ Kanz scored 23 while
Lead Software Engineer @ Synechron scored 2."
```

---

### Task 4: Seniority, employer, and freshness

**Files:**
- Modify: `job_scoring.py`
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `seniority_fit(job: dict) -> float`, `employer_fit(job: dict) -> float`,
  `freshness(job: dict, *, now: datetime | None = None) -> float`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_job_scoring.py
from datetime import datetime, timedelta, timezone


def test_seniority_fit_peaks_in_his_band():
    assert job_scoring.seniority_fit(job(min_experience=6)) == 1.0
    assert job_scoring.seniority_fit(job(min_experience=3)) == 0.6
    assert job_scoring.seniority_fit(job(min_experience=-1)) == 0.6


def test_employer_fit_prefers_a_direct_employer_over_an_agency():
    direct = job(company="Acme", company_website="https://acme.example", recruiter_company="")
    agency = job(company="Acme", recruiter_company="Dicetek LLC")
    unknown = job(company="Acme", company_website="", recruiter_company="")
    assert job_scoring.employer_fit(direct) == 1.0
    assert job_scoring.employer_fit(agency) == 0.3
    assert job_scoring.employer_fit(unknown) == 0.6


def test_employer_fit_reads_an_aggregator_note_as_an_agency():
    reposted = job(credibility_notes="posted via TalentPool aggregator")
    assert job_scoring.employer_fit(reposted) == 0.3


def test_freshness_decays_over_the_seven_day_window():
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    def posted(days):
        return job(date_posted=(now - timedelta(days=days)).isoformat())

    assert job_scoring.freshness(posted(1), now=now) == 1.0
    assert job_scoring.freshness(posted(3), now=now) == 0.7
    assert job_scoring.freshness(posted(6), now=now) == 0.4


def test_freshness_of_an_undated_posting_is_the_middle_band():
    assert job_scoring.freshness(job(date_posted="")) == 0.7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: FAIL with `AttributeError: module 'job_scoring' has no attribute 'seniority_fit'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to job_scoring.py

# He has 7+ years. A posting asking 5-8 is aimed at him; one asking 3 is aimed
# lower and usually pays lower. Above 8 was already knocked out.
SENIORITY_BANDS = ((5, 8, 1.0), (3, 4, 0.6), (0, 2, 0.3))
UNSTATED_SENIORITY = 0.6

# Extracted min_experience takes the smallest year figure anywhere in the text,
# so it understates. Weight this dimension accordingly: 15 of 100.
AGENCY_MARKERS = ("aggregator", "agency", "recruitment", "staffing", "consultancy")

FRESHNESS_BANDS = ((0, 2, 1.0), (3, 4, 0.7), (5, 7, 0.4))
UNDATED_FRESHNESS = 0.7


def seniority_fit(job):
    """How well the years asked for match the years he has, 0.0-1.0."""
    years = job.get("min_experience", -1)
    if not isinstance(years, int) or years < 0:
        return UNSTATED_SENIORITY
    for low, high, value in SENIORITY_BANDS:
        if low <= years <= high:
            return value
    return 0.3


def employer_fit(job):
    """Whether the employer is hiring directly, 0.0-1.0."""
    company = str(job.get("company") or "").strip().lower()
    recruiter = str(job.get("recruiter_company") or "").strip().lower()
    notes = str(job.get("credibility_notes") or "").lower()

    if recruiter and recruiter != company:
        return 0.3
    if any(marker in notes for marker in AGENCY_MARKERS):
        return 0.3
    if str(job.get("company_website") or "").strip():
        return 1.0
    return 0.6


def freshness(job, *, now=None):
    """How recently it was posted, 0.0-1.0."""
    raw = str(job.get("date_posted") or "").strip()
    if not raw:
        return UNDATED_FRESHNESS
    try:
        posted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return UNDATED_FRESHNESS
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    days = ((now or datetime.now(timezone.utc)) - posted).days
    for low, high, value in FRESHNESS_BANDS:
        if low <= days <= high:
            return value
    return 0.2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: PASS, 21 tests

- [ ] **Step 5: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Add seniority, employer, and freshness dimensions

Employer type was invisible to the old scorer, so a Dicetek repost and a
direct posting from the same company ranked identically."
```

---

### Task 5: The weighted total

**Files:**
- Modify: `job_scoring.py`
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: every dimension from Tasks 3 and 4, and `knockout` from Task 2.
- Produces: `WEIGHTS: dict[str, int]`, `BANDS`, `band(total: int) -> str`,
  `evaluate(job, *, allowed_locations, max_experience=8, seen_keys=frozenset(), now=None) -> dict`
  with keys `passed` (bool), `reason` (str|None), `total` (int 0-100),
  `band` (str), `parts` (dict of dimension name to float).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_job_scoring.py
def test_weights_sum_to_one_hundred():
    assert sum(job_scoring.WEIGHTS.values()) == 100


def test_evaluate_returns_a_knocked_out_job_with_its_reason_and_no_score():
    result = job_scoring.evaluate(job(title="DevOps Manager"), allowed_locations=UAE)
    assert result["passed"] is False
    assert "devops" in result["reason"]
    assert result["total"] == 0


def test_evaluate_scores_a_strong_match_into_the_excellent_band():
    strong = job(
        title="Backend Lead - Microservices Architect",
        tech_required="kotlin, spring boot, microservices, kubernetes, aws",
        min_experience=6, company_website="https://purecs.example",
        date_posted=datetime.now(timezone.utc).isoformat(),
    )
    result = job_scoring.evaluate(strong, allowed_locations=UAE)
    assert result["passed"] is True
    assert result["total"] >= 75
    assert result["band"] == "excellent"


def test_evaluate_puts_a_generic_title_below_the_send_cutoff():
    weak = job(title="software engineer", tech_required="php", min_experience=2,
               company_website="", recruiter_company="Kanz Recruitment")
    result = job_scoring.evaluate(weak, allowed_locations=UAE)
    assert result["total"] < 45


def test_evaluate_reports_every_dimension_so_a_score_can_be_explained():
    result = job_scoring.evaluate(job(), allowed_locations=UAE)
    assert set(result["parts"]) == set(job_scoring.WEIGHTS)


def test_band_boundaries():
    assert job_scoring.band(75) == "excellent"
    assert job_scoring.band(74) == "good"
    assert job_scoring.band(60) == "good"
    assert job_scoring.band(59) == "normal"
    assert job_scoring.band(45) == "normal"
    assert job_scoring.band(44) == "below"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: FAIL with `AttributeError: module 'job_scoring' has no attribute 'WEIGHTS'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to job_scoring.py

# Config, not code: these are meant to be refitted against labelled ratings
# once there are enough of them. Stack and role dominate by his own choice.
WEIGHTS = {
    "stack": 35,
    "role": 30,
    "seniority": 15,
    "employer": 12,
    "freshness": 8,
}

SEND_CUTOFF = 45
BANDS = ((75, "excellent"), (60, "good"), (45, "normal"))


def band(total):
    """The display band for a total. Below the cutoff is never sent."""
    for floor, name in BANDS:
        if total >= floor:
            return name
    return "below"


def evaluate(job, *, allowed_locations, max_experience=8, seen_keys=frozenset(), now=None):
    """Knockouts, then the weighted dimensions. Always returns every part."""
    reason = knockout(
        job,
        allowed_locations=allowed_locations,
        max_experience=max_experience,
        seen_keys=seen_keys,
    )
    empty = {name: 0.0 for name in WEIGHTS}
    if reason:
        return {"passed": False, "reason": reason, "total": 0, "band": "knocked out", "parts": empty}

    parts = {
        "stack": stack_fit(job),
        "role": role_fit(job),
        "seniority": seniority_fit(job),
        "employer": employer_fit(job),
        "freshness": freshness(job, now=now),
    }
    total = round(sum(parts[name] * WEIGHTS[name] for name in WEIGHTS))
    return {"passed": True, "reason": None, "total": total, "band": band(total), "parts": parts}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: PASS, 27 tests

- [ ] **Step 5: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Combine the dimensions into one explainable 0-100 score

Every score now carries the five numbers it was built from, so a ranking
can be argued with instead of only trusted."
```

---

### Task 6: Measure it against the labelled jobs

**Files:**
- Create: `tools/eval_scoring.py`
- Test: `tests/test_eval_scoring.py`

**Interfaces:**
- Consumes: `job_scoring.evaluate` from Task 5.
- Produces: `auc(positives: list[float], negatives: list[float]) -> float`,
  `load_labels(db_path: str) -> tuple[list[dict], list[dict]]`,
  `report(db_path: str) -> dict` with keys `auc`, `baseline_auc`, `n_positive`,
  `n_negative`, `above_cutoff`. Runnable as `python tools/eval_scoring.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_eval_scoring.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import eval_scoring


def test_auc_is_one_when_every_positive_outranks_every_negative():
    assert eval_scoring.auc([90, 80, 70], [60, 50]) == 1.0


def test_auc_is_zero_when_the_ranking_is_exactly_backwards():
    assert eval_scoring.auc([10, 20], [80, 90]) == 0.0


def test_auc_counts_a_tie_as_half_a_win():
    assert eval_scoring.auc([50], [50]) == 0.5


def test_auc_of_an_empty_side_is_undefined_and_reported_as_a_half():
    assert eval_scoring.auc([], [70]) == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_eval_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eval_scoring'`

- [ ] **Step 3: Write minimal implementation**

```python
# tools/eval_scoring.py
"""Measure a scorer against the jobs he actually judged.

AUC is the chance a job he marked interested outranks one he skipped. 0.5 is a
coin flip. The keyword scorer this replaces measured 0.565.
"""

import itertools
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import job_scoring

BASELINE_AUC = 0.565
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"
MARKETS = ("dubai", "abu dhabi", "jeddah", "switzerland", "riyadh",
           "saudi", "united arab emirates", "sharjah")

# Rows written by the test harness, not by the scraper. They would otherwise
# dominate: one of them carries a hand-set score of 99.
FAKE = ("JobHunter Test", "Example FinTech", "Example SaaS")


def auc(positives, negatives):
    """Chance a positive outranks a negative, ties counting half."""
    if not positives or not negatives:
        return 0.5
    wins = sum(1 for a, b in itertools.product(positives, negatives) if a > b)
    ties = sum(1 for a, b in itertools.product(positives, negatives) if a == b)
    return (wins + 0.5 * ties) / (len(positives) * len(negatives))


def load_labels(db_path=DEFAULT_DB):
    """Return (interested, skipped) job dicts, excluding test fixtures."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(FAKE))
    sql = (
        f"SELECT * FROM jobs WHERE status = ? AND company NOT IN ({placeholders}) "
        "AND title NOT LIKE 'TEST RUN%' AND title NOT LIKE 'CTA Button%'"
    )
    interested = [dict(r) for r in conn.execute(sql, ("interested", *FAKE))]
    skipped = [dict(r) for r in conn.execute(sql, ("skipped", *FAKE))]
    conn.close()
    return interested, skipped


def report(db_path=DEFAULT_DB):
    """Score both label sets and return the comparison against the baseline."""
    interested, skipped = load_labels(db_path)

    def totals(jobs):
        return [job_scoring.evaluate(j, allowed_locations=MARKETS)["total"] for j in jobs]

    positives, negatives = totals(interested), totals(skipped)
    above = sum(1 for t in positives if t >= job_scoring.SEND_CUTOFF)
    return {
        "auc": round(auc(positives, negatives), 3),
        "baseline_auc": BASELINE_AUC,
        "n_positive": len(positives),
        "n_negative": len(negatives),
        "above_cutoff": f"{above}/{len(positives)}",
    }


if __name__ == "__main__":
    for key, value in report().items():
        print(f"{key:14} {value}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_eval_scoring.py -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Run the measurement for real**

Run: `.venv/bin/python tools/eval_scoring.py`
Expected: prints `auc`, `baseline_auc 0.565`, `n_positive 10`, `n_negative 34`, `above_cutoff`.

Record the number. If `auc` is at or below 0.565 the rubric is not yet better
than what it replaces — do not proceed to Task 7. Report the figure and the
per-dimension parts for the ten interested jobs, and stop for a decision.

- [ ] **Step 6: Commit**

```bash
git add tools/eval_scoring.py tests/test_eval_scoring.py
git commit -m "Measure the scorer against the jobs he actually judged

AUC on 10 interested against 34 skipped. The keyword scorer measured
0.565, where a coin flip is 0.500, so the bar is not 'looks reasonable'."
```

---

### Task 7: Use it in the scraper

**Files:**
- Modify: `scraper.py:1814` (the threshold comparison in the collection loop)
- Modify: `scraper.py` (`score_job`)
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: `job_scoring.evaluate` from Task 5.
- Produces: `scraper.score_job(job) -> tuple[int, list[str]]` keeping its existing
  signature and return shape, so `save_job` and every existing caller and test are unaffected.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_job_scoring.py
import scraper


def test_scraper_score_job_delegates_to_the_rubric():
    strong = {
        "title": "Backend Lead - Microservices Architect", "company": "PureCS",
        "location": "Dubai, United Arab Emirates",
        "tech_required": "kotlin, spring boot, microservices, kubernetes, aws",
        "tech_nice_to_have": "", "min_experience": 6, "description": "",
        "company_website": "https://purecs.example", "recruiter_company": "",
        "date_posted": "",
    }
    score, breakdown = scraper.score_job(strong)
    assert score >= 75
    assert any("stack" in line for line in breakdown)


def test_scraper_score_job_returns_zero_for_a_knocked_out_job():
    score, breakdown = scraper.score_job({
        "title": "DevOps Manager", "company": "MODSOFT",
        "location": "Dubai, United Arab Emirates", "tech_required": "aws, azure",
        "tech_nice_to_have": "", "min_experience": 2, "description": "",
    })
    assert score == 0
    assert any("devops" in line for line in breakdown)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: FAIL — the old scorer returns a positive score for `DevOps Manager`.

- [ ] **Step 3: Replace the body of `score_job` in `scraper.py`**

Delete the whole existing `score_job` body and put this in its place. Add
`import job_scoring` beside the other imports at the top of the file.

```python
def score_job(job):
    """Score a job 0-100 with the rubric in job_scoring.

    Signature and return shape are unchanged: every caller, save_job included,
    still receives (score, breakdown-lines).
    """
    result = job_scoring.evaluate(
        job,
        allowed_locations=tuple(loc.lower() for loc in CONFIG.get("allowed_locations", ())),
        max_experience=CONFIG.get("max_experience", 8),
    )
    if not result["passed"]:
        return 0, [f"knocked out: {result['reason']}"]

    breakdown = [
        f"{name} {result['parts'][name]:.2f}x{job_scoring.WEIGHTS[name]}"
        for name in job_scoring.WEIGHTS
    ]
    breakdown.append(f"band {result['band']}")
    return result["total"], breakdown
```

Then change the threshold in `CONFIG` from `"score_threshold": 15` to
`"score_threshold": 45` so it matches the new scale, and confirm the comparison
at `scraper.py:1814` still reads `result["score"] >= CONFIG["score_threshold"]`.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS. The suite was 183 tests before this plan and gains 29 from it.
Any pre-existing test that asserts a specific old score is a real finding —
report which test and what it asserted rather than editing it to match.

- [ ] **Step 5: Re-measure**

Run: `.venv/bin/python tools/eval_scoring.py`
Expected: the same AUC as Task 6. A change here means the scraper path and the
evaluation path disagree, which is a bug in this task.

- [ ] **Step 6: Commit**

```bash
git add scraper.py tests/test_job_scoring.py
git commit -m "Score postings with the rubric instead of counting keywords

Threshold moves from 15 to 45 with the scale. score_job keeps its
signature, so save_job and every existing caller are untouched."
```

---

### Task 8: Deduplicate within a run

**Files:**
- Modify: `scraper.py` (the `evaluate_job` closure in the collection loop, around line 1765)
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: `job_scoring.duplicate_key` from Task 2.
- Produces: no new public functions; the collection loop stops storing repeats.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_job_scoring.py
def test_duplicate_key_collapses_a_repost_of_the_same_role():
    a = {"title": "Senior Technical Architect", "company": "Inception"}
    b = {"title": "Technical Architect", "company": "Inception"}
    c = {"title": "Technical Architect", "company": "Different Co"}
    assert job_scoring.duplicate_key(a) == job_scoring.duplicate_key(b)
    assert job_scoring.duplicate_key(a) != job_scoring.duplicate_key(c)
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -k duplicate -v`
Expected: PASS — `duplicate_key` already exists from Task 2. This test pins the
behaviour the scraper change depends on; if it fails, fix Task 2 before going on.

- [ ] **Step 3: Track seen keys through the run**

In `scraper.py`, immediately before the `def evaluate_job(job):` closure, add:

```python
    # Titles already stored this run. Inception posted one architect role three
    # times in a single night; three rows would take three of the daily slots.
    seen_titles = set()
```

Inside `evaluate_job`, directly after the `if is_job_seen(conn, job["id"]):`
guard, add:

```python
        title_key = job_scoring.duplicate_key(job)
        if title_key in seen_titles:
            log.info(f"Skipped (repost of a title already stored): {job['title']} @ {job['company']}")
            return None
```

and immediately before `save_job(conn, job)` near the end of the closure, add:

```python
        seen_titles.add(title_key)
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS

- [ ] **Step 5: Verify against the real database**

Run:
```bash
.venv/bin/python -c "
import sqlite3, collections, sys
sys.path.insert(0, '.')
import job_scoring
c = sqlite3.connect('data/jobs.db')
keys = collections.Counter(job_scoring.duplicate_key({'title': t, 'company': co})
                           for t, co in c.execute('SELECT title, company FROM jobs'))
dupes = {k: n for k, n in keys.items() if n > 1}
print('duplicate title+company groups:', len(dupes))
print('rows that would collapse:', sum(n - 1 for n in dupes.values()))
"
```
Expected: a non-zero count, confirming the deduplication has real work to do.
Report both numbers.

- [ ] **Step 6: Commit**

```bash
git add scraper.py tests/test_job_scoring.py
git commit -m "Stop storing the same role twice in one scrape

Inception posted one architect role three times in a single night."
```

---

### Task 9: Refit the weights against his ratings

Run this task only once the 60 labels exist. Until then it is blocked, and the
hand-set weights from Task 5 ship as they are.

**Files:**
- Create: `tools/fit_weights.py`
- Test: `tests/test_fit_weights.py`

**Interfaces:**
- Consumes: `job_scoring.evaluate` and `WEIGHTS` from Task 5, `eval_scoring.auc` from Task 6.
- Produces: `grid(step: int = 5) -> list[dict[str, int]]`,
  `fit(labels: list[tuple[dict, str]], *, step: int = 5, holdout: float = 0.33) -> dict`
  with keys `weights`, `train_auc`, `test_auc`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fit_weights.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fit_weights


def test_every_candidate_weighting_sums_to_one_hundred():
    for weights in fit_weights.grid(step=20):
        assert sum(weights.values()) == 100


def test_the_grid_is_exhaustive_at_its_step():
    coarse = fit_weights.grid(step=50)
    assert {"stack": 100, "role": 0, "seniority": 0, "employer": 0, "freshness": 0} in coarse
    assert {"stack": 50, "role": 50, "seniority": 0, "employer": 0, "freshness": 0} in coarse
    assert all(sum(w.values()) == 100 for w in coarse)


def test_fit_holds_out_part_of_the_labels_from_training():
    labels = [({"title": f"Architect {i}", "company": "A", "location": "Dubai",
                "tech_required": "java, spring boot", "min_experience": 6,
                "description": "", "company_website": "https://a.example",
                "recruiter_company": "", "date_posted": "", "tech_nice_to_have": ""},
               "excellent" if i % 2 else "bad") for i in range(20)]
    result = fit_weights.fit(labels, step=25)
    assert set(result) == {"weights", "train_auc", "test_auc"}
    assert sum(result["weights"].values()) == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fit_weights.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fit_weights'`

- [ ] **Step 3: Write minimal implementation**

```python
# tools/fit_weights.py
"""Grid-search the dimension weights against his own ratings.

The weights in job_scoring are hand-set. With enough labelled jobs a search
beats a guess, but only if it is measured on labels it never trained on --
five weights fitted on forty examples overfit easily.
"""

import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_scoring
import job_scoring

DIMENSIONS = ("stack", "role", "seniority", "employer", "freshness")
GOOD_LABELS = ("good", "excellent")
MARKETS = eval_scoring.MARKETS


def grid(step=5):
    """Every weighting on `step` boundaries that sums to 100."""
    values = range(0, 101, step)
    out = []
    for stack, role, seniority, employer in itertools.product(values, repeat=4):
        freshness = 100 - (stack + role + seniority + employer)
        if 0 <= freshness <= 100:
            out.append({
                "stack": stack, "role": role, "seniority": seniority,
                "employer": employer, "freshness": freshness,
            })
    return out


def _total(job, weights):
    parts = job_scoring.evaluate(job, allowed_locations=MARKETS)["parts"]
    return sum(parts[name] * weights[name] for name in DIMENSIONS)


def fit(labels, *, step=5, holdout=0.33, seed=11):
    """Search the grid on a training split, report on a held-out split."""
    shuffled = list(labels)
    random.Random(seed).shuffle(shuffled)
    cut = max(1, int(len(shuffled) * (1 - holdout)))
    train, test = shuffled[:cut], shuffled[cut:]

    def split(rows):
        pos = [j for j, label in rows if label in GOOD_LABELS]
        neg = [j for j, label in rows if label not in GOOD_LABELS]
        return pos, neg

    train_pos, train_neg = split(train)
    test_pos, test_neg = split(test)

    best, best_auc = dict(job_scoring.WEIGHTS), -1.0
    for weights in grid(step=step):
        score = eval_scoring.auc(
            [_total(j, weights) for j in train_pos],
            [_total(j, weights) for j in train_neg],
        )
        if score > best_auc:
            best, best_auc = weights, score

    held = eval_scoring.auc(
        [_total(j, best) for j in test_pos],
        [_total(j, best) for j in test_neg],
    )
    return {"weights": best, "train_auc": round(best_auc, 3), "test_auc": round(held, 3)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fit_weights.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Report, do not auto-apply**

Run: `.venv/bin/python tools/fit_weights.py` after wiring a `__main__` block
that loads the 60 ratings and prints `fit(labels)`.

If `test_auc` is well below `train_auc`, the search overfit and the hand-set
weights stand. Report both numbers and the fitted weighting; changing
`job_scoring.WEIGHTS` is a decision for the user, not this task.

- [ ] **Step 6: Commit**

```bash
git add tools/fit_weights.py tests/test_fit_weights.py
git commit -m "Fit the dimension weights against his ratings, held-out split

Five weights on sixty labels overfit trivially, so the search trains on
two thirds and is judged on the third it never saw."
```

---

### Task 10: Title-seniority knockout

**Spec:** `docs/superpowers/specs/2026-09-03-job-scoring-design.md`, addendum
"Title-seniority knockout (Task 10)".

**Files:**
- Modify: `job_scoring.py` (`LITERAL_BLOCKS`, new `SENIOR_TITLE_MODIFIERS`, `blocked_title`)
- Test: `tests/test_job_scoring.py`

**Interfaces:**
- Consumes: `_WORD`, `_INFLECTION`, `blocked_title(job)` (signature unchanged),
  `LITERAL_BLOCKS`, all defined in Task 1.
- Produces: `SENIOR_TITLE_MODIFIERS: tuple[str, ...]`. `blocked_title` gains a
  new reason shape, `"blocked title: too senior (<word>)"`.

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_job_scoring.py`, and update the two existing
tests that assert on `LITERAL_BLOCKS`' current shape (`staff engineer` is
moving out of it):

```python
def test_blocked_title_catches_over_senior_modifiers():
    for title in ("Principal Technical Architect", "Expert Solution Architect",
                  "Enterprise Data & Cloud Solutions Architect",
                  "Staff Architect", "Staff Backend Engineer"):
        assert job_scoring.blocked_title({"title": title}), title


def test_blocked_title_names_the_seniority_modifier():
    assert (job_scoring.blocked_title({"title": "Principal Architect"})
            == "blocked title: too senior (principal)")


def test_blocked_title_still_keeps_management_track_titles_he_might_want():
    for title in ("Head of Engineering", "Lead Software Engineer", "Engineering Manager"):
        assert job_scoring.blocked_title({"title": title}) is None, title
```

Replace the existing `test_blocked_title_honours_the_four_literal_blocks`
(it tests `Staff Engineer` alongside the three `LITERAL_BLOCKS` entries that
are staying — `Staff Engineer` moves to the new modifier test above):

```python
def test_blocked_title_honours_the_three_literal_blocks():
    for title in ("Senior Architect", "Senior Cloud Architect",
                  "Senior Lead Software Engineer"):
        assert job_scoring.blocked_title({"title": title}), title
```

And update the existing reason-string assertion in
`test_blocked_title_says_which_rule_rejected_it` — `Staff Engineer` no longer
matches a `LITERAL_BLOCKS` phrase, it matches the new modifier:

```python
def test_blocked_title_says_which_rule_rejected_it():
    assert job_scoring.blocked_title({"title": "DevOps Manager"}) == "blocked role family: devops"
    assert job_scoring.blocked_title({"title": "Staff Engineer"}) == "blocked title: too senior (staff)"
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: the three new tests FAIL (`SENIOR_TITLE_MODIFIERS` / new reason
shape don't exist yet); `test_blocked_title_honours_the_three_literal_blocks`
passes already (its three titles are untouched); the updated
`test_blocked_title_says_which_rule_rejected_it` FAILS on the `Staff Engineer`
assertion.

- [ ] **Step 3: Measure the word list against the real corpus**

Run this before touching `job_scoring.py`, to catch a false positive before
it ships rather than after:

```bash
.venv/bin/python - <<'PY'
import re
import sqlite3

pattern = re.compile(r"\b(principal|expert|enterprise|staff)\b")
conn = sqlite3.connect("data/jobs.db")
titles = [row[0] for row in conn.execute("SELECT DISTINCT title FROM jobs")]
hits = sorted({t for t in titles if pattern.search(str(t).lower())})
for t in hits:
    print(t)
print(f"\n{len(hits)} distinct titles match")
PY
```

Read every title in the output. If any looks like a role he would actually
want (not just "senior-sounding" — a real false positive, the same bar every
other knockout in this file was held to), STOP and report it rather than
silently narrowing the word list. This is a corpus measurement, not a
decision — a real conflict is a ruling for the user, same as every prior
task's corpus findings in the ledger.

- [ ] **Step 4: Write the implementation**

In `job_scoring.py`, remove `"staff engineer"` and `"staff software engineer"`
from `LITERAL_BLOCKS`:

```python
LITERAL_BLOCKS = (
    "senior architect", "senior cloud architect", "senior lead software engineer",
)
```

Add the new constant directly below `LITERAL_BLOCKS`, and a matching pattern
tuple next to `_FAMILY_PATTERNS`:

```python
# Individual-contributor titles above the level he wants, confirmed directly
# with him rather than inferred: "too senior" was his most common skip reason
# (10 of 25 ratings) and the rubric had no way to see it. Distinct from
# SENIORITY_WORDS: those are management-track words normalise_title strips so
# the underlying role family can judge them (Engineering Manager stays a
# role_fit call, not a knockout, per the Task 1 ruling). These four are
# individual-contributor words with no such ambiguity -- he ruled them out
# directly, regardless of company or role.
SENIOR_TITLE_MODIFIERS = ("principal", "expert", "enterprise", "staff")

_SENIOR_MODIFIER_PATTERNS = tuple(
    (word, re.compile(rf"\b{re.escape(word)}{_INFLECTION}\b"))
    for word in SENIOR_TITLE_MODIFIERS
)
```

Wire it into `blocked_title`, checked on the raw lowercased title alongside
`LITERAL_BLOCKS` and before the family-pattern loop:

```python
def blocked_title(job):
    """Return a reason string when the title is one he never wants, else None."""
    raw = str(job.get("title") or "").lower()
    for phrase in LITERAL_BLOCKS:
        if phrase in raw:
            return f"blocked title: {phrase}"
    for word, pattern in _SENIOR_MODIFIER_PATTERNS:
        if pattern.search(raw):
            return f"blocked title: too senior ({word})"

    normalised = normalise_title(raw)
    rescuable = None  # computed once, and only if a rescuable family matches
    for family, pattern in _FAMILY_PATTERNS:
        for match in pattern.finditer(normalised):
            if family in RESCUABLE_FAMILIES and _QUALIFIED_BY.search(normalised[:match.start()]):
                if rescuable is None:
                    rescuable = _names_work_he_wants(raw, normalised)
                if rescuable:
                    continue  # a qualifier on the role, not the role itself
            return f"blocked role family: {family}"
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_job_scoring.py -v`
Expected: PASS, all tests including the three new ones.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q tests`
Expected: PASS, no regressions. Report the before/after count.

- [ ] **Step 7: Commit**

```bash
git add job_scoring.py tests/test_job_scoring.py
git commit -m "Knock out Principal/Expert/Enterprise/Staff titles as too senior

Confirmed directly with him: 'too senior' was his most common skip reason
(10 of 25 ratings) and the rubric had no concept of it. These four are
individual-contributor seniority words he ruled off the table regardless
of company; management-track words (Head of, Lead) stay a role_fit
judgment, unchanged."
```

---

## What this plan does not cover

Sponsorship beyond the explicit-refusal knockout in Task 2. The spec puts the
real judgment in stage 2, where a model reads the description and the market
prior; 97.7% of postings say nothing, so nothing else belongs in stage 1.

Delivery: the stage-2 AI review contract with its sponsorship field, the top-3
per market cap with spillover, the carry-over queue, and the Telegram digest
with drill-down callbacks. Those depend on what the scorer produces and get
their own plan once Task 6 reports an AUC above the 0.565 baseline.
