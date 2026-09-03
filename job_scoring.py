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
    "embedded", "firmware", "hardware", "frontend", "front-end", "front end",
    "ui engineer", "ui developer", "ux engineer", "react", "angular", "vue",
)

# Exact strings he asked to block. These stay literal: stripping "senior" from
# "senior cloud architect" would turn it back into a title he wants.
LITERAL_BLOCKS = (
    "staff engineer", "staff software engineer", "senior architect",
    "senior cloud architect", "senior lead software engineer",
)

_WORD = re.compile(r"[a-z0-9+#.-]+")

# A family word may carry an inflection in a title: "manual tester",
# "networking engineer", "reactjs developer". The list is closed so a short
# entry such as "qa" can never match inside an unrelated word like "qatar".
_INFLECTION = r"(?:s|es|er|ers|ing|js|\.js)?"
_FAMILY_PATTERNS = tuple(
    (family, re.compile(rf"\b{re.escape(family)}{_INFLECTION}\b"))
    for family in ROLE_FAMILY_BLOCKS
)


def normalise_title(title):
    """Lowercase a title and drop the words that only describe seniority."""
    words = _WORD.findall(str(title or "").lower())
    # "Sr." and "Front-End" must land on the same tokens as "Sr" and "Front-End",
    # so drop a trailing dot and any hyphen that is not joining two words.
    tokens = [w.strip("-").rstrip(".") for w in words]
    kept = [w for w in tokens if w and w not in SENIORITY_WORDS]
    return " ".join(kept)


def blocked_title(job):
    """Return a reason string when the title is one he never wants, else None."""
    raw = str(job.get("title") or "").lower()
    for phrase in LITERAL_BLOCKS:
        if phrase in raw:
            return f"blocked title: {phrase}"

    normalised = normalise_title(raw)
    for family, pattern in _FAMILY_PATTERNS:
        if pattern.search(normalised):
            return f"blocked role family: {family}"
    return None


# Promoted from a -3 penalty to a knockout: as points it was defeatable by
# buzzword count, so junior roles with a dense stack list still got through.
JUNIOR_WORDS = (
    "junior", "intern", "interns", "internship", "entry level", "entry-level",
    "graduate", "fresh graduate", "trainee", "apprentice",
)

# The rare posting that says outright it will not sponsor. 2 of 4,575 postings
# in the corpus match anything like this, so it is a safety net, not the gate.
REFUSES_SPONSORSHIP = (
    "no visa sponsorship", "will not sponsor", "won't sponsor",
    "does not sponsor", "unable to sponsor", "not able to sponsor",
    "no relocation", "local candidates only", "local hires only",
)


def _words(text):
    """Lowercase text as space-joined tokens, so phrases match whole words only."""
    return " ".join(w.rstrip(".") for w in _WORD.findall(str(text or "").lower()))


# German postings tag the title with "(m/w/d)" and variants. The same role
# posted with and without the tag must land on one key, so strip it first.
_GENDER_MARKER = re.compile(r"\(?\b[mwfdx](?:\s*/\s*[mwfdx]){1,3}\b\)?")


def duplicate_key(job):
    """Identity of a posting for deduplication: normalised title plus company."""
    title = _GENDER_MARKER.sub(" ", str(job.get("title") or "").lower())
    return f"{normalise_title(title)}|{_words(job.get('company'))}"


def knockout(job, *, allowed_locations, max_experience=8, seen_keys=frozenset()):
    """Return the reason this job is rejected outright, or None to keep it.

    Runs before any scoring. No number of matching keywords overturns one of
    these, which is the whole point of separating them from the score.
    """
    reason = blocked_title(job)
    if reason:
        return reason

    # Whole-word match: "intern" must not reject "International Architect".
    title = f" {_words(job.get('title'))} "
    for word in JUNIOR_WORDS:
        if f" {word} " in title:
            return f"too junior: {word}"

    location = str(job.get("location") or "").lower()
    if allowed_locations and not any(a in location for a in allowed_locations):
        return f"outside the configured markets: {job.get('location') or 'unknown'}"

    years = job.get("min_experience", -1)
    if isinstance(years, int) and years > max_experience:
        return f"wants {years}+ years, over the {max_experience} cap"

    # 85 of 4,580 real descriptions use a curly apostrophe, which would make
    # "won't sponsor" a dead entry. Fold it to the straight form before matching.
    description = str(job.get("description") or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    for phrase in REFUSES_SPONSORSHIP:
        if phrase in description:
            return f"refuses sponsorship: {phrase}"

    if duplicate_key(job) in seen_keys:
        return "duplicate of a posting already seen"

    return None


# His stack in three rings. Core is what he is hired for; cloud is the platform
# he builds on; adjacent is credible but not differentiating.
CORE_STACK = ("java", "kotlin", "spring boot", "spring", "microservices")
CLOUD_STACK = ("aws", "azure", "terraform", "kubernetes", "docker")
ADJACENT_STACK = (
    "nestjs", "typescript", ".net", "c#", "postgresql", "mongodb", "redis",
    "kafka", "rabbitmq", "rest", "graphql", "grpc", "event-driven", "ddd",
)

# Spellings the rings do not list, folded onto the term they mean before
# coverage is counted. Listing "k8s" as a sixth cloud term would instead
# dilute every posting's cloud fraction. Add aliases here, not to the rings.
STACK_ALIASES = {"k8s": "kubernetes"}
_ALIAS_PATTERNS = tuple(
    (re.compile(rf"\b{re.escape(alias)}\b"), canonical)
    for alias, canonical in STACK_ALIASES.items()
)


def _stack_text(value):
    """A tech list lowercased, with each alias replaced by its ring term."""
    text = str(value or "").lower()
    for pattern, canonical in _ALIAS_PATTERNS:
        text = pattern.sub(canonical, text)
    return text


# A technology the posting requires counts fully; one it merely likes counts
# less. The old scorer gave a flat +1 for any nice-to-have match at all.
NICE_TO_HAVE_CREDIT = 0.4

# Ordered: the first family whose pattern matches the title wins, so "full
# stack architect" resolves to full-stack rather than architect. "platform
# architect" is not listed separately: "architect" claims it, which is right.
# "architecture" earns the same rung: 16 of the 24 corpus titles that carry the
# word without "architect" own the architecture ("Cloud Solution Architecture",
# "Director IT Architecture"), against 8 that merely mention it.
ROLE_FAMILIES = (
    (0.4, ("full stack", "fullstack", "full-stack")),
    (1.0, ("architect", "architecture", "tech lead", "technical lead",
           "software lead", "lead software engineer", "lead backend",
           "backend lead")),
    # Every spelling, as the frontend block does: 14 corpus titles write
    # "Back End" or "Back-End" and were read as generic engineers.
    (0.8, ("backend engineer", "back-end engineer", "back end engineer",
           "backend developer", "back-end developer", "back end developer",
           "backend software engineer", "back-end software engineer",
           "back end software engineer",
           "software engineer backend", "software engineer back-end",
           "software engineer back end")),
    (0.5, ("engineering manager", "head of engineering", "vp engineering",
           "director of engineering", "cto")),
)
GENERIC_ROLE_FIT = 0.3

# Whole words only, as the blocked families are matched. As a substring "cto"
# sat inside "director", "sector" and "contractor", which put 75 sales and
# management titles in the corpus on the CTO rung.
_ROLE_PATTERNS = tuple(
    (value, tuple(re.compile(rf"\b{re.escape(phrase)}{_INFLECTION}\b") for phrase in phrases))
    for value, phrases in ROLE_FAMILIES
)


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
    required = _stack_text(job.get("tech_required"))
    optional = _stack_text(job.get("tech_nice_to_have"))
    return min(1.0, (
        0.60 * _ring_coverage(CORE_STACK, required, optional)
        + 0.30 * _ring_coverage(CLOUD_STACK, required, optional)
        + 0.10 * _ring_coverage(ADJACENT_STACK, required, optional)
    ) / 0.62)


def role_fit(job):
    """How close the title is to the work he wants, 0.0-1.0."""
    # Both forms are searched: "Tech Lead" only survives in the raw title
    # because normalising strips "lead", while "Software Engineer, Backend"
    # only reads as "software engineer backend" once the comma is gone.
    title = normalise_title(job.get("title"))
    raw = str(job.get("title") or "").lower()
    for value, patterns in _ROLE_PATTERNS:
        for pattern in patterns:
            if pattern.search(title) or pattern.search(raw):
                return value
    return GENERIC_ROLE_FIT


# He has 7+ years. A posting asking 5-8 is aimed at him; one asking 3 is aimed
# lower and usually pays lower. Above 8 was already knocked out.
#
# Extracted min_experience takes the smallest year figure anywhere in the text,
# so it understates: "5+ years backend, 2+ years Kubernetes" records 2. That
# is why this dimension is worth only 15 of 100.
SENIORITY_BANDS = ((5, 8, 1.0), (3, 4, 0.6), (0, 2, 0.3))
UNSTATED_SENIORITY = 0.6

# credibility_notes is written only by scraper.py, from a fixed set of phrases.
# "posted by agency/aggregator" and "posted via <company> aggregator" are the
# two that name an agency employer; both carry "agency" or "aggregator".
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
