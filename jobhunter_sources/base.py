"""Shared contract and helpers for the Spain job boards.

A board module exposes three names:

``BOARD``
    A :class:`Board` describing how the scraper and the availability checker
    talk to the site.
``scrape(session, keyword, location, *, get, config)``
    A generator yielding one list of job dicts per listing page, in the same
    shape ``scraper.scrape_linkedin`` yields: ``id``, ``title``, ``company``,
    ``location``, ``url``, ``source`` and ``date_posted`` (ISO 8601 or "").
    ``keyword`` is ``None`` for boards without keyword search; they list the
    city and keep only titles that :func:`title_matches_search` accepts.
    ``get(url, **kwargs)`` is the scraper's rate-limited GET; ``config`` is
    ``scraper.CONFIG``. Never raise on HTTP errors: log and stop.
``fetch_description(session, job, *, get)``
    Returns the posting body as plain text ("" when unavailable) and may set
    ``job["apply_url"]`` (the employer's own posting or ATS form) and
    ``job["language_requirement"]`` (what the board says about Spanish, as
    plain text) when the detail page exposes them. Estimated salaries shown
    by the board are never copied into the body: ``scraper.extract_salary``
    must only see what the employer wrote.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import unicodedata

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Board:
    """What the scraper, the digest and the availability checker know about a board."""

    name: str
    """``job["source"]``: the display name on Telegram cards."""
    key: str
    """Lowercase identity used by the availability checker and job id prefix (``f"{key}-"``)."""
    module: str
    """Import path of the board module."""
    keyword_search: bool
    """One generator per keyword and city when True; one per city otherwise."""
    countries: frozenset
    """job_scoring country codes the board can search; a profile with no
    configured location in them never calls the board."""
    hosts: frozenset
    """Hostnames whose job URLs the availability checker recognizes."""
    canonical_host: str
    """Host used to rebuild a clean listing URL for availability checks."""
    job_path: str
    """Regex for a job page path; group 1 is the board's own job id."""
    selectors: dict = field(default_factory=dict)
    """``header``, ``company`` and ``description`` CSS selectors for availability checks."""
    apply_labels: tuple = ()
    """Extra visible apply-control labels (lowercase, exact) the board uses."""
    high_priority: bool = False
    """English-speaking tech boards the owner asked to search first."""

    @property
    def prefix(self):
        return f"{self.key}-"


SPANISH_CITIES = {
    "valencia": "Valencia", "valència": "Valencia", "madrid": "Madrid", "barcelona": "Barcelona",
}

# Titles a city listing keeps when the board has no keyword search. Loose on
# purpose: ``scraper.is_excluded`` and the rubric do the real filtering, and
# LinkedIn's own keyword search is no stricter than this.
DEFAULT_TITLE_TERMS = (
    "architect", "arquitect", "lead", "backend", "back-end", "back end", "engineer", "developer",
    "desarrollador", "ingeniero", "programador", "java", "kotlin", "node", "spring", "typescript",
)


def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(c))


def city_of(location):
    """The chosen Spanish city a scraper location names, or None.

    ``"Valencia, Spain"`` -> ``"Valencia"``. Boards receive the scraper's
    region strings and turn them into their own slugs with this.
    """
    head = str(location or "").split(",")[0].strip().lower()
    return SPANISH_CITIES.get(head) or SPANISH_CITIES.get(strip_accents(head))


def city_slug(location):
    city = city_of(location)
    return strip_accents(city).lower() if city else None


def title_matches_search(title, config=None):
    """Whether a listing title is worth a detail fetch on a keyword-less board.

    An invited (generic) profile keeps titles matching its own search terms,
    not the owner's defaults.
    """
    config = config or {}
    terms = config.get("city_listing_title_terms")
    if not terms and config.get("matching", {}).get("preset") == "generic":
        terms = [strip_accents(term).lower() for term in config.get("keywords") or ()]
    elif not terms:
        terms = DEFAULT_TITLE_TERMS
    folded = strip_accents(title).lower()
    return any(term in folded for term in terms)


def spanish_location(card_location, queried_location):
    """A display location that ``job_scoring.location_allowed`` accepts.

    The board filed the posting under the queried city; its own location
    text wins when it names a chosen city, otherwise the queried city is
    used so a card that only says "Remote" or "Hybrid" still lands in the
    market it was listed in.
    """
    card = " ".join(str(card_location or "").split())
    city = city_of(card) if card else None
    if city is None:
        for name in SPANISH_CITIES.values():
            if re.search(rf"(?<!\w){name}(?!\w)", strip_accents(card), re.I):
                city = name
                break
    if city is None:
        placeless = re.sub(r"\b(?:remote|remoto|teletrabajo|hybrid|híbrido|hibrido|spain|españa|espana|full|time|part)\b|[^\w]",
                           "", strip_accents(card), flags=re.I)
        if placeless:
            # Another place entirely (Seville, Lisbon): keep the board's own
            # text so the location filter can reject it.
            return card
        city = city_of(queried_location)
    if city is None:
        return card
    suffix = ""
    if re.search(r"\b(?:remote|remoto|teletrabajo)\b", card, re.I):
        suffix = " (remote)"
    elif re.search(r"\b(?:hybrid|híbrido|hibrido)\b", card, re.I):
        suffix = " (hybrid)"
    return f"{city}, Spain{suffix}"


_BLOCKS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
           "dt", "dd", "blockquote", "pre", "hr", "table", "header", "footer")


def clean_text(html_fragment):
    """Plain text with line breaks between blocks, the way the LinkedIn body reads."""
    if html_fragment is None:
        return ""
    if isinstance(html_fragment, str):
        soup = BeautifulSoup(html_fragment, "lxml")
    else:
        soup = html_fragment
    for node in soup.select("script, style, noscript, template"):
        node.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCKS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text("")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def json_ld_job_postings(soup):
    """Every JSON-LD JobPosting node on a page, flattening @graph lists."""
    found = []

    def walk(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                found.append(value)
            for key in ("@graph", "mainEntity", "itemListElement", "item"):
                if key in value:
                    walk(value[key], depth + 1)

    for script in soup.select("script[type='application/ld+json']"):
        try:
            walk(json.loads(script.string or script.get_text() or ""))
        except (ValueError, TypeError, RecursionError):
            continue
    return found


_RELATIVE = re.compile(
    r"(?P<n>\d+|a|an|un|una|hace)?\s*(?P<unit>minute|min|hour|hr|day|week|month|minuto|hora|día|dia|semana|mes)e?s?\b", re.I)
_UNIT_DAYS = {"minute": 0, "min": 0, "minuto": 0, "hour": 0, "hr": 0, "hora": 0, "day": 1, "día": 1, "dia": 1,
              "week": 7, "semana": 7, "month": 30, "mes": 30}


def iso_date(value, *, now=None):
    """Turn what boards print as a posting date into ISO 8601, or "".

    Accepts ISO strings, ``YYYY-MM-DD``, epoch seconds/milliseconds, and
    relative phrases such as "3 days ago", "today", "yesterday", "hace 2 días".
    """
    if value is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = " ".join(str(value).split()).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        pass
    lowered = strip_accents(text).lower()
    if re.search(r"\b(?:today|hoy|just now|new|nuevo|nueva)\b", lowered):
        return now.isoformat()
    if re.search(r"\b(?:yesterday|ayer)\b", lowered):
        return (now - timedelta(days=1)).isoformat()
    match = _RELATIVE.search(lowered)
    if match and re.search(r"\b(?:ago|hace)\b", lowered):
        count = match.group("n")
        count = 1 if count in (None, "a", "an", "un", "una", "hace") else int(count)
        unit = strip_accents(match.group("unit").lower())
        days = _UNIT_DAYS.get(unit)
        if days is not None:
            return (now - timedelta(days=count * days)).isoformat()
    for pattern, order in ((r"(\d{1,2})/(\d{1,2})/(\d{4})", "dmy"), (r"(\d{4})-(\d{2})-(\d{2})", "ymd")):
        match = re.search(pattern, text)
        if match:
            parts = [int(p) for p in match.groups()]
            year, month, day = (parts[2], parts[1], parts[0]) if order == "dmy" else parts
            try:
                return datetime(year, month, day, tzinfo=timezone.utc).isoformat()
            except ValueError:
                return ""
    return ""


def stable_id(board, raw):
    """``<key>-<board id>`` with the board id kept readable but safe."""
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw or "")).strip("-")
    return f"{board.prefix}{token}" if token else ""
