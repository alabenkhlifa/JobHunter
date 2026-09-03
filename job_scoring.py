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

# Two family words name a technology at least as often as they name a role,
# and blocking them bare vetoed titles he searches for by name: "cloud
# architect" is one of his own search keywords, yet `Cloud Infrastructure
# Architect` was blocked. These, and only these, can be rescued. `network` is
# deliberately absent: rescuing it freed no corpus title (29 blocked before
# and after) while `Cloud Network Architect` and `Tech Lead - Cloud Network`
# slipped through on the word `architect` alone — all of the escape risk and
# none of the gain.
RESCUABLE_FAMILIES = ("infrastructure", "frontend", "front-end", "front end")

# A rescuable family word that directly follows one of these is a qualifier on
# a role he wants rather than the role itself. The test is adjacency, not mere
# co-occurrence: `Cloud Infrastructure Architect` is a cloud architect,
# `Infrastructure Architect` is an infrastructure architect, and `Software
# Engineer - Frontend` is still a frontend job however the title is arranged.
FAMILY_QUALIFIERS = (
    "cloud", "software", "solution", "solutions", "backend", "back-end",
    "microservices", "api", "application", "applications",
)

# Separators survive normalisation as their own tokens ("+" in "Microservices
# + front end + Cloud"), so allow them between the qualifier and the family.
_QUALIFIED_BY = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in FAMILY_QUALIFIERS) + r")"
    r"(?:\s+[^a-z0-9\s]+)*\s+$"
)


def _names_work_he_wants(raw_title, normalised):
    """True when a title names one of his role heads or a core technology.

    The second half of the rescue. A qualifier alone is not enough: "Cloud
    Infrastructure Lead" is an infrastructure job whatever precedes the word,
    and without this it was rescued into a score of 53 and sent. Derived from
    ROLE_FAMILIES and CORE_STACK rather than restated, so the rescue cannot
    drift away from the rubric it is protecting. Both are defined below; the
    reference resolves at call time.
    """
    if role_fit({"title": raw_title}) >= 0.8:
        return True
    return any(re.search(rf"\b{re.escape(term)}\b", normalised) for term in CORE_STACK)


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


# Promoted from a -3 penalty to a knockout: as points it was defeatable by
# buzzword count, so junior roles with a dense stack list still got through.
#
# A knockout is only as good as its spellings. "jr" sat in SENIORITY_WORDS and
# not here, so `Jr Backend Engineer` scored 84 and was sent while
# `Junior Backend Engineer` was knocked out — one abbreviation deciding whether
# a junior role reached his phone. Every form below is one the corpus actually
# writes: `Jr. Architect- Modeler` (3 rows), `Software Engineer (Fresh
# Graduates)`, `Fresher Software Engineer`, `Fresh -Technical architect`.
#
# Ordered most specific first, so the entry that matches is also the entry that
# names the reason and no longer phrase is shadowed by a shorter one inside it.
# That ordering is what brings "fresh graduate" back to life: it sat behind
# "graduate", which claimed every match, and could never fire.
JUNIOR_WORDS = (
    "junior-level", "entry-level", "entry level", "graduate level",
    "fresh graduate", "fresh graduates", "graduates", "graduate",
    "junior", "jr", "freshers", "fresher", "fresh",
    "internship", "interns", "intern",
    "trainee", "apprenticeship", "apprentice",
)

# The rare posting that says outright it will not sponsor. 2 of 4,575 postings
# in the corpus match anything like this, so it is a safety net, not the gate.
REFUSES_SPONSORSHIP = (
    "no visa sponsorship", "will not sponsor", "won't sponsor",
    "does not sponsor", "unable to sponsor", "not able to sponsor",
    "sponsorship is not available", "not offer sponsorship",
    "no relocation", "local candidates only", "local hires only",
)
# Deliberately absent: "without sponsorship". 12 of its 13 corpus hits are
# Cloudflare's US export-control boilerplate ("...may be hired without
# sponsorship"), not a visa refusal, so it would reject jobs on a phrase that
# does not mean what the list is for.


# He is a software architect. The Gulf boards are full of building architects,
# and the title alone cannot tell them apart: LITERAL_BLOCKS carries "senior
# architect" for that reason, but "Senior Technical Architect" does not contain
# it, so a design practice hiring for "large residential developments including
# branded residences and luxury hospitality" scored 59 at full role marks.
# 148 of the 988 corpus titles carrying "architect" are building-industry
# postings, so this is the rule, not the exception.
#
# The description decides, because the title is what fails.
#
# Every term below was measured against the corpus: it must not appear in a
# genuine software posting. The ones that did are named in the rejects note
# after the list, so the next person adding a term reads why they were left
# out before adding one of them back.
BUILDING_MARKERS = (
    # Drafting and visualisation tools no software team installs.
    "revit", "autocad", "auto cad", "archicad", "navisworks", "sketchup", "lumion",
    # Deliverables and disciplines of a design practice.
    "architectural drawing", "shop drawing", "interior design",
    "landscape architecture", "landscape architect", "masterplanning", "masterplan",
    "curtain wall", "quantity surveyor", "built environment",
    # The project types this corpus advertises them against.
    "luxury villa", "residential development",
    # Accreditation and codes: RIBA is the UK architects' body, Estidama is
    # Abu Dhabi's building rating system.
    "riba", "estidama", "building regulation",
)
# Considered and rejected, each because it fires on real software work:
#   facade      the Facade pattern — no corpus row uses it that way yet, which
#               is luck, not safety
#   bim         3 of its 10 sole-marker rows are software-vendor jobs (Oracle
#               and Autodesk construction products, an Eaton data-centre SA)
#   3ds max     a Senior Character Rigger at a robotics company
#   civil engineer / structural engineer
#               41 sole-marker rows, one of them a lunar-lander test bench
#   ifc         "issued for construction", the BIM file format, and the IFC
#   bill of quantities, tender, contractor
#               procurement words: 51-68% of their corpus rows are software
#   mep, hvac   building services, and hvac alone would take a Schneider
#               Electric IoT/BMS solution architect with it
#   rhino       Mozilla Rhino and Rhino Mocks are both software
#   autodesk, aia, rics, leed
#               vendor and certification names that sit in software postings
#   architectural design, design development, concept design, master planning,
#   schematic design, setting out
#               ordinary software-design and SAP/MRP English
#   fit-out     ambulances and vehicles are fitted out too
#   snagging, joinery
#               AV installation work and a company name

# Vocabulary that proves a posting is software work whatever else it mentions.
# Deliberately not derived from CORE_STACK/CLOUD_STACK/ADJACENT_STACK: those
# describe HIS stack, this must recognise ANY software job, and ADJACENT_STACK
# holds "rest", which the building posting that started this matches on the
# English "the rest of the year".
#
# This half is what makes the markers safe. A CAD vendor building CAD tools is
# a real category and one he could take: the corpus holds a "Senior BIM / IFC
# Software Engineer" that requires Revit knowledge, and it is spared because it
# also asks for Python, GitLab and CI/CD. 19 marker-carrying rows are spared
# this way.
SOFTWARE_EVIDENCE = (
    "java", "kotlin", "spring boot", "spring framework", "microservice",
    "kubernetes", "docker", "terraform", "ansible",
    "aws", "azure", "gcp", "google cloud",
    "python", "typescript", "javascript", "node.js", "nodejs", "golang",
    "c#", ".net", "php", "ruby", "react", "angular", "vue.js",
    "django", "flask", "fastapi",
    "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
    "graphql", "grpc", "rest api", "restful",
    "ci/cd", "devops", "git", "github", "gitlab", "jenkins", "sql", "api",
    "software engineer", "software engineering", "software development",
    "software developer", "software architect", "source code", "codebase",
    "sdlc", "saas", "machine learning", "iot", "cybersecurity",
    "enterprise architecture", "backend", "back-end", "frontend", "front-end",
    "full stack", "full-stack", "linux", "unix", "unit test",
    "object-oriented", "algorithm", "programming", "data pipeline", "etl",
    "application development",
)
# Bare "software" is absent on purpose: "architectural software such as
# AutoCAD" and "the Autodesk software suite" would spare the very postings
# this is here to catch.


def _phrase_pattern(term, suffix=""):
    """A whole-word pattern for a phrase, hyphen and space reading the same.

    The same treatment _find_tech_in_text gives a configured technology, so
    "fit-out" and "fit out" or "auto cad" and "auto-cad" cannot diverge.
    """
    parts = re.split(r"[\s-]+", term.strip())
    body = r"[\s-]+".join(re.escape(part) for part in parts)
    return re.compile(rf"(?<!\w){body}{suffix}(?!\w)", re.IGNORECASE)


# The markers carry an inflection because the corpus writes both numbers:
# "luxury villas", "residential developments", "interior designer". The
# evidence terms do not, because they are product names.
#
# Word boundaries matter most for the short ones. "bim" would otherwise read
# out of "bim360" and "bimm"; it is not in the list, but the next short marker
# someone adds will be, and this is what keeps it honest.
_BUILDING_PATTERNS = tuple(
    (marker, _phrase_pattern(marker, _INFLECTION)) for marker in BUILDING_MARKERS
)
_SOFTWARE_PATTERNS = tuple(_phrase_pattern(term) for term in SOFTWARE_EVIDENCE)


def building_industry(job):
    """Return a reason when the description is a building-architecture job, else None.

    Two-sided on purpose. A marker alone is not enough, because a company that
    sells software to architects writes the same words; the posting must also
    show no software vocabulary at all. That is why this can never be defeated
    by, and can never defeat, a genuine backend posting: one marker term is not
    a knockout, one marker term with nothing technical anywhere in the
    description is.
    """
    description = str(job.get("description") or "")
    if not description:
        return None
    for marker, pattern in _BUILDING_PATTERNS:
        if pattern.search(description):
            if any(evidence.search(description) for evidence in _SOFTWARE_PATTERNS):
                return None
            return f"building industry, not software: {marker}"
    return None


def _words(text):
    """Lowercase text as space-joined tokens, so phrases match whole words only."""
    return " ".join(w.rstrip(".") for w in _WORD.findall(str(text or "").lower()))


# German postings tag the title with "(m/w/d)" and variants. The same role
# posted with and without the tag must land on one key, so strip it first.
_GENDER_MARKER = re.compile(r"\(?\b[mwfdx](?:\s*/\s*[mwfdx]){1,3}\b\)?")


def duplicate_key(job):
    """Identity of a posting for deduplication: normalised title, company, country.

    The country, not the city: the same title at one company in Dubai and in
    Riyadh is two jobs he could take, in two countries he chose separately,
    so collapsing them would hide one. Dubai and Abu Dhabi are one repost.
    """
    title = _GENDER_MARKER.sub(" ", str(job.get("title") or "").lower())
    return f"{normalise_title(title)}|{_words(job.get('company'))}|{market_country(job.get('location'))}"


# The five markets he chose, matched as substrings of the displayed location,
# with the Swiss city and language spellings for postings that omit the
# country. scraper.CONFIG["allowed_locations"] and tools/eval_scoring.py both
# read it from here, so the number the tool measures is the number the
# scraper ships. Sharjah and a bare "United Arab Emirates" are not here on
# purpose: the boards return them for these searches, and he did not pick
# them. Jeddah and Riyadh are cities, not the country: "saudi" would bring
# Dammam and Khobar with it. "jiddah" is one board's spelling of Jeddah, on
# 17 of the 4,580 corpus rows; every Riyadh row in the corpus spells it
# "riyadh", so that one term covers all 989 of them.
#
# Grouped by country because duplicate_key needs the country a market is in;
# DEFAULT_MARKETS is the same flat tuple every caller always read.
MARKET_COUNTRIES = {
    "uae": ("dubai", "abu dhabi"),
    "ksa": ("jeddah", "jiddah", "riyadh"),
    "ch": (
        "switzerland", "schweiz", "suisse", "svizzera",
        "zurich", "zürich", "geneva", "genève", "genf",
        "basel", "bern", "lausanne", "zug", "lucerne", "luzern",
    ),
}
DEFAULT_MARKETS = tuple(term for terms in MARKET_COUNTRIES.values() for term in terms)

# Names that place a location in a country without naming a chosen market.
# They decide the country segment of duplicate_key only, never whether a
# market is allowed: a bare "United Arab Emirates" is still knocked out. 862
# of the 4,580 corpus rows name only the country or an unchosen city, and
# without these the same Dubai title split between "uae" and "unknown".
COUNTRY_NAMES = {
    "uae": ("united arab emirates", "uae"),
    "ksa": ("saudi arabia", "saudi"),
}


def market_country(location):
    """The country a displayed location falls in, matched the way knockout matches markets.

    "unknown" when nothing places it, so every undetermined location lands
    in one bucket instead of fragmenting the duplicate key.
    """
    location = str(location or "").lower()
    for country, terms in MARKET_COUNTRIES.items():
        if any(term in location for term in terms + COUNTRY_NAMES.get(country, ())):
            return country
    return "unknown"


def knockout(job, *, allowed_locations, max_experience=8, seen_keys=frozenset()):
    """Return the reason this job is rejected outright, or None to keep it.

    Runs before any scoring. No number of matching keywords overturns one of
    these, which is the whole point of separating them from the score.

    `seen_keys` is not passed by the scraper. It enforces the same rule in
    its collection loop, before the description fetch, so a repost costs no
    network round trip — over a set seeded from the database across the
    freshness window, not one that dies with the process. The parameter is
    for other callers and the tests.
    """
    reason = blocked_title(job)
    if reason:
        return reason

    # Whole-word match: "intern" must not reject "International Architect".
    # A hyphen joins two tokens into one, so "Junior-Level Developer" reads as
    # a single "junior-level" token and " junior " would miss it. Match against
    # the split spelling too, which is also how normalise_title reads a title.
    title = f" {_words(job.get('title'))} "
    unhyphenated = title.replace("-", " ")
    for word in JUNIOR_WORDS:
        if f" {word} " in title or f" {word} " in unhyphenated:
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

    reason = building_industry(job)
    if reason:
        return reason

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
# Extracted min_experience reads the headline figure, not the smallest one:
# "5+ years backend, 2+ years Kubernetes" records 5. It used to record 2, and
# that understatement is the reason this dimension is worth only 15 of 100.
# The weight was fitted while it understated, so it is now conservative.
SENIORITY_BANDS = ((5, 8, 1.0), (3, 4, 0.6), (0, 2, 0.3))
UNSTATED_SENIORITY = 0.6

# credibility_notes is written only by scraper.py, from a fixed set of
# phrases. "posted by agency/aggregator" and "posted via <company> aggregator"
# are the two that name an agency employer; nothing it writes contains
# "recruitment", "staffing" or "consultancy", so those would be dead entries.
AGENCY_MARKERS = ("aggregator", "agency")

# Names and words that mark an intermediary rather than the hiring company.
# Derived from the company column of data/jobs.db on 2026-09-03 (4,580 rows,
# 1,728 distinct names): each generic word was checked against every name it
# matches there and hit only agencies, boards and outsourcers; each named entry
# is an agency or aggregator the corpus carries, most with "our client" in
# the descriptions. Matched as whole words, so "talent" flags "MCG Talent" and
# "Talents Tide" but not a product name with the string inside it. No
# "consulting", "consultancy", "careers", "agency" or "jobs": Cognizant
# Consulting, Tata Consultancy Services, BCG, NAFFCO Careers, a government
# or creative agency, and the 60 "Confidential ..." placeholders that speak
# as the employer all hire directly, so the consulting-named agencies and
# the job boards are listed by name instead.
AGENCY_COMPANY_TERMS = (
    "recruitment", "recruit", "staffing", "talent", "hr solutions",
    "outsourcing", "manpower", "headhunting", "placement", "executive search",
    "talentmate", "jobgether", "halian", "dicetek", "dautom",
    "penta consulting", "nexus consulting", "yo it consulting",
    "avensys consulting", "hyve technology consulting",
    "accel human resource consultants", "agile consultants", "hired",
    "jobs ai", "women first jobs", "senior it jobs uk", "jobs via efinancialcareers",
)
_AGENCY_PATTERNS = tuple(
    re.compile(rf"\b{re.escape(term)}{_INFLECTION}\b") for term in AGENCY_COMPANY_TERMS
)

# In this corpus a company that is not an agency is the direct employer, so
# the absence of every agency signal scores full marks. A middle "unknown"
# tier would take 4.8 points off every posting and shift the cutoff.
DIRECT_EMPLOYER = 1.0
AGENCY_EMPLOYER = 0.3

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
    """Whether the employer is hiring directly, 0.0-1.0.

    company_website is not read: no scraper fills it today, and with two
    tiers a website could only ever confirm what the absence of an agency
    signal already says.

    ACCEPTED ORDERING: in the live scraper the first two branches almost
    never fire. `recruiter_company` and `credibility_notes` are written by
    scraper.save_job, which runs AFTER score_job, so a job is scored before
    either field exists; `recruiter_company` is empty on all 4,580 stored
    rows. Live scoring therefore reduces to the company-name list below.
    That is accepted rather than fixed: generating the notes before scoring
    would pull scraper's coarser AGENCY_OR_AGGREGATOR_TERMS in through the
    back door — its "consulting" entry alone re-flags Cognizant Consulting
    and Boston Consulting Group, the exact false positives this list was
    trimmed to avoid — while closing a gap worth 2 rows in 4,580. The two
    branches stay for callers that re-score stored rows, and
    tools/eval_scoring.py blanks both fields so the measured number is the
    shipped one.
    """
    company = str(job.get("company") or "").strip().lower()
    recruiter = str(job.get("recruiter_company") or "").strip().lower()
    notes = str(job.get("credibility_notes") or "").lower()

    if recruiter and recruiter != company:
        return AGENCY_EMPLOYER
    if any(marker in notes for marker in AGENCY_MARKERS):
        return AGENCY_EMPLOYER
    if any(pattern.search(company) for pattern in _AGENCY_PATTERNS):
        return AGENCY_EMPLOYER
    return DIRECT_EMPLOYER


def freshness(job, *, now=None):
    """How recently it was posted, 0.0-1.0."""
    raw = str(job.get("date_posted") or "").strip()
    if not raw:
        return UNDATED_FRESHNESS
    # 3 corpus rows carry the word instead of a date.
    if raw.lower() == "today":
        days = 0
    else:
        try:
            posted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return UNDATED_FRESHNESS
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        # A posting dated tomorrow is not stale: clock skew or a source's
        # timezone put it in the future, so read it as posted today.
        days = max(0, ((now or datetime.now(timezone.utc)) - posted).days)
    for low, high, value in FRESHNESS_BANDS:
        if low <= days <= high:
            return value
    return 0.2


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
BANDS = ((75, "excellent"), (60, "good"), (SEND_CUTOFF, "normal"))


def band(total):
    """The display band for a total. Below the cutoff is never sent."""
    for floor, name in BANDS:
        if total >= floor:
            return name
    return "below"


def evaluate(job, *, allowed_locations, max_experience=8, seen_keys=frozenset(), now=None):
    """Knockouts, then the weighted dimensions. Always returns every part.

    Neither `passed` nor `band` is the send gate; `sendable` is. `passed`
    means the job survived the knockouts, not that it clears the cutoff, so
    a passed job can still land in the "below" band. `band` is for display
    only: a knocked-out job carries "knocked out", which is not in BANDS.

    `now`, when given, must be timezone-aware: `freshness` subtracts it from
    an aware posting date and raises TypeError for a naive one.
    """
    reason = knockout(
        job,
        allowed_locations=allowed_locations,
        max_experience=max_experience,
        seen_keys=seen_keys,
    )
    if reason:
        empty = {name: 0.0 for name in WEIGHTS}
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


def sendable(result):
    """Whether an `evaluate` result reaches the user: total at or above the cutoff.

    The one gate callers should use. A knocked-out job has total 0, so it
    fails here too, without the caller reading `passed` or `band`.
    """
    return result["total"] >= SEND_CUTOFF
