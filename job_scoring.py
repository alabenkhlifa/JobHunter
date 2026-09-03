"""Scoring for job postings: knockouts first, then five weighted dimensions.

Pure functions over plain dicts shaped like rows of the `jobs` table. This
module imports nothing from scraper.py so it can be tested without a database.
"""

import re

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

    description = str(job.get("description") or "").lower()
    for phrase in REFUSES_SPONSORSHIP:
        if phrase in description:
            return f"refuses sponsorship: {phrase}"

    if duplicate_key(job) in seen_keys:
        return "duplicate of a posting already seen"

    return None
