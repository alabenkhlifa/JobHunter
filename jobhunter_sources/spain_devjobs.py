"""Spain Dev Jobs (https://spain-devjobs.com): English-speaking tech jobs in Spain.

The board lists only employers whose working language is English and takes
every posting from the employer's own careers page; salaries appear only
when the employer published one. Pages are server-rendered HTML with no
login or bot wall. It shares one job database with sister boards
(germanydevjobs.com, francedevjobs.com, poland-devjobs.com, gulfdevjobs.com),
so a job page here may name a sister site as its canonical URL; the same
slug and id identify the same job there.

URLs used:

``/jobs?city=<slug>&sort=newest[&page=N]``
    One newest-first listing per city, 20 cards a page. Pagination follows
    ``nav.pagination a[rel=next]`` and stops at the last page, on any error,
    at ``max_pages``, or after a page whose cards are all older than
    ``max_job_age_days``. The board's ``q`` search matches whole
    descriptions (a "software architect" query in Valencia returned a
    mechanical engineer), so it is not used: titles are filtered locally.
``/jobs/<company>-<title>-<id>``
    The job page: the employer's text, the exact posting date, the
    employer's website and the board's key facts (Spanish, English, visa,
    relocation, apply-from).

Deliberately not fetched: ``/go/<uuid>``, the Apply redirect to the
employer's posting. robots.txt disallows it and each request counts as an
apply click, so it is stored as ``apply_url`` for a person to follow. Nor
``/api``, ``/stats``, the RSS feed (newest 50 jobs in Spain, no city filter)
or the sitemap. The server sends no charset; pages are UTF-8.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from . import base

log = logging.getLogger("scraper")

ROOT = "https://spain-devjobs.com"
SPAIN_HOSTS = frozenset({"spain-devjobs.com", "www.spain-devjobs.com"})
SISTER_HOSTS = frozenset(f"{prefix}{host}" for host in (
    "germanydevjobs.com", "francedevjobs.com", "poland-devjobs.com", "gulfdevjobs.com") for prefix in ("", "www."))
_GO = re.compile(r"https://spain-devjobs\.com/go/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_YEARS = re.compile(r"\b(\d+|a|an|one) years? ago\b", re.I)
_LANGUAGE_FACTS = ("Spanish", "English")
_BOARD_FACTS = ("Apply from", "Visa sponsorship", "Relocation support")


BOARD = base.Board(
    name="Spain Dev Jobs",
    key="sdj",
    module="jobhunter_sources.spain_devjobs",
    keyword_search=False,
    countries=frozenset({"es"}),
    # Sister hosts let a job page whose canonical URL is on a sister site
    # still prove its identity to jobhunter_availability.
    hosts=SPAIN_HOSTS | SISTER_HOSTS,
    canonical_host="spain-devjobs.com",
    # Group 1 is the trailing board id, exactly what stable_id() receives.
    job_path=r"/jobs/(?:[^/?#]+-)?([a-z0-9]{7,12})/?",
    selectors={
        "header": "header.job-header",
        "company": ".job-header__company > span:not(.company-logo)",
        "description": "article .job-description",
    },
    # The button reads "Apply on <Employer> website"; the shared APPLY pattern
    # accepts that and drops the screen-reader "(opens in a new tab)" suffix.
    apply_labels=("apply",),
    high_priority=True,
)


def _page(get, url):
    """The parsed page, or None after logging why not. Never raises."""
    try:
        resp = get(url)
    except requests.RequestException as error:
        log.warning(f"{BOARD.name}: request failed for {url}: {error}")
        return None
    if getattr(resp, "status_code", None) != 200:
        log.warning(f"{BOARD.name}: {url} returned {getattr(resp, 'status_code', None)}")
        return None
    resp.encoding = "utf-8"  # sent without a charset; requests would guess ISO-8859-1
    try:
        return BeautifulSoup(resp.text or "", "lxml")
    except (TypeError, ValueError) as error:
        log.warning(f"{BOARD.name}: unreadable page {url}: {error}")
        return None


def _lower_first(text):
    """Lower-case a capitalised first word: "No Spanish required" -> "no Spanish required"; "B2" stays."""
    return text[:1].lower() + text[1:] if re.match(r"[A-Z][a-z]", text) else text


def _posted(text, now):
    """Card date ("Yesterday", "2 weeks ago", "1 year ago") as ISO 8601, or ""."""
    value = base.iso_date(text, now=now)
    match = None if value else _YEARS.search(str(text or ""))
    if match:
        years = int(match.group(1)) if match.group(1).isdigit() else 1
        value = (now - timedelta(days=365 * years)).isoformat()
    return value


def _older(job, max_age, now):
    try:
        posted = datetime.fromisoformat(job["date_posted"])
    except (TypeError, ValueError):
        return False
    return (now - posted).days > max_age


def _job_url(href):
    """Absolute job URL and board id for a card link, or (None, None)."""
    try:
        url = urljoin(f"{ROOT}/", str(href or "")).split("#")[0].split("?")[0]
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # "http://[bad" and other malformed links
        return None, None
    match = re.fullmatch(BOARD.job_path, parts.path)
    if parts.scheme != "https" or host not in SPAIN_HOSTS or not match:
        return None, None
    return f"{ROOT}{parts.path.rstrip('/')}", match.group(1)


def _card(row, location, now):
    link = row.select_one("a.job-row__link[href]")
    title = row.select_one(".job-row__title")
    if link is None or title is None:
        return None
    url, board_id = _job_url(link["href"])
    if url is None:
        return None
    company = row.select_one(".job-row__company")
    salary = row.select_one(".job-row__salary")
    # Meta spans are [company, location, (salary), relative date].
    plain = [span.get_text(" ", strip=True) for span in row.select(".job-row__meta > span")
             if not {"job-row__company", "job-row__salary"} & set(span.get("class", []))]
    badges = {}
    for badge in row.select(".job-badges .badge"):
        kind = next((c[len("badge--"):] for c in badge.get("class", []) if c.startswith("badge--")), "other")
        badges.setdefault(kind, badge.get_text(" ", strip=True))
    tag = row.select_one("[data-opened-tag]")
    apply_url = f"{ROOT}/go/{tag['data-opened-tag'].strip().lower()}" if tag else ""
    job = {
        "id": base.stable_id(BOARD, board_id),
        "title": title.get_text(" ", strip=True),
        "company": company.get_text(" ", strip=True) if company else "Unknown",
        "location": base.spanish_location(plain[0] if len(plain) >= 2 else "", location),
        "url": url,
        "source": BOARD.name,
        "date_posted": _posted(plain[-1] if plain else "", now),
        "apply_url": apply_url if _GO.fullmatch(apply_url) else "",
        "language_requirement": f"board: {_lower_first(badges['english'])}" if badges.get("english") else "",
    }
    facts = [text for kind, text in badges.items() if kind in ("remote", "visa", "relocation", "overseas") and text]
    if facts:
        job["board_facts"] = "; ".join(facts)
    if salary is not None and salary.get_text(strip=True):
        job["salary_text"] = salary.get_text(" ", strip=True)  # employer-published; the board never estimates
    return job


def _next_url(soup, current):
    link = soup.select_one("nav.pagination a[rel~=next][href]")
    if link is None:
        return None
    try:
        url = urljoin(current, link["href"])
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        log.warning(f"{BOARD.name}: ignoring malformed next link {link['href']!r}")
        return None
    if parts.scheme != "https" or host not in SPAIN_HOSTS or parts.path != "/jobs":
        log.warning(f"{BOARD.name}: ignoring unexpected next link {url}")
        return None
    return url


def scrape(session, keyword, location, *, get, config):
    """Yield the city's listing one page at a time, keeping titles worth a detail fetch.

    ``keyword`` is ignored (the board is searched per city, see the module
    docstring); a location outside the chosen cities makes no request.
    """
    slug = base.city_slug(location)
    if slug is None:
        log.debug(f"{BOARD.name}: no city listing for {location!r}")
        return
    max_pages = max(1, int(config.get("max_pages") or 1))
    max_age = config.get("max_job_age_days")
    url, seen = f"{ROOT}/jobs?city={slug}&sort=newest", set()
    for page in range(1, max_pages + 1):
        log.info(f"{BOARD.name}: {slug} page {page}")
        soup = _page(get, url)
        if soup is None:
            return
        rows = soup.select("section.jobs-page__results li.job-row")
        if not rows:
            log.info(f"{BOARD.name}: no more results")
            return
        now = datetime.now(timezone.utc)
        cards = []
        for row in rows:
            try:
                card = _card(row, location, now)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                log.debug(f"{BOARD.name}: skipping card: {error}")
                continue
            if card is not None and card["id"] not in seen:
                seen.add(card["id"])
                cards.append(card)
        jobs = [job for job in cards if base.title_matches_search(job["title"], config)]
        log.info(f"{BOARD.name}: kept {len(jobs)} of {len(rows)} cards on page {page}")
        yield jobs
        dated = [card for card in cards if card["date_posted"]]
        if max_age is not None and not dated:
            # The card date format changed: without dates nothing stops the
            # crawl short of max_pages, and evaluate_job cannot drop old cards
            # before their detail fetch. The newest page is enough.
            log.warning(f"{BOARD.name}: no readable card dates on page {page}, stopping")
            return
        if max_age is not None and all(_older(card, max_age, now) for card in dated):
            log.info(f"{BOARD.name}: page {page} is older than {max_age} days, stopping")
            return
        url = _next_url(soup, url)
        if url is None:
            return


def _key_facts(soup):
    facts = {}
    cells = soup.select(".job-header dl.key-facts .key-facts__cell") or soup.select("dl.key-facts .key-facts__cell")
    for cell in cells:
        term, value = cell.find("dt"), cell.find("dd")
        if term is not None and value is not None:
            facts.setdefault(term.get_text(" ", strip=True), value.get_text(" ", strip=True))
    return facts


def _posting(soup, job):
    """The page's JSON-LD JobPosting when its url is this job."""
    wanted = _job_url(job.get("url"))[1]
    for node in base.json_ld_job_postings(soup):
        if wanted and _job_url(node.get("url"))[1] == wanted:
            return node
    return {}


def _enrich(soup, job):
    posting = _posting(soup, job)
    posted = base.iso_date(posting.get("datePosted")) if isinstance(posting.get("datePosted"), str) else ""
    stamp = soup.select_one(".job-header__meta time[datetime]")
    posted = posted or (base.iso_date(stamp["datetime"]) if stamp is not None else "")
    if posted:
        job["date_posted"] = posted
    org = posting.get("hiringOrganization")
    site = org.get("sameAs") if isinstance(org, dict) else ""
    site = site[0] if isinstance(site, list) and site else site
    try:
        parts = urlsplit(site) if isinstance(site, str) else None
        if parts is not None and parts.scheme in ("http", "https") and parts.hostname:
            job["company_website"] = site
    except ValueError:  # a malformed sameAs must not cost the description
        pass
    facts = _key_facts(soup)
    language = [f"{name} {_lower_first(facts[name])}" for name in _LANGUAGE_FACTS if facts.get(name)]
    if language:
        job["language_requirement"] = "board: " + "; ".join(language)
    # Visa and relocation wording stays out of the description, where
    # "Visa sponsorship: Not stated" would reach sponsorship_signal.
    board_facts = [f"{name}: {facts[name]}" for name in _BOARD_FACTS if facts.get(name)]
    if board_facts:
        job["board_facts"] = "; ".join(board_facts)
    salary = facts.get("Salary", "")
    if salary and salary.casefold() != "not disclosed" and not job.get("salary_text"):
        job["salary_text"] = salary
    if not job.get("apply_url"):
        button = soup.select_one("header.job-header a.apply-btn[href]")
        apply_url = urljoin(f"{ROOT}/", button["href"]) if button is not None else ""
        if _GO.fullmatch(apply_url):
            job["apply_url"] = apply_url
    node = soup.select_one("article .job-description")
    if node is not None:
        text = base.clean_text(node)
    else:
        body = posting.get("description")
        text = base.clean_text(body) if isinstance(body, str) else ""
    skills = list(dict.fromkeys(a.get_text(" ", strip=True) for a in soup.select("section.chip-links a")))
    skills = [skill for skill in skills if skill]
    if text and skills:
        text += "\n\nSkills: " + ", ".join(skills)
    return text


def fetch_description(session, job, *, get):
    """The employer's text plus "Skills: ...", or ""; enriches ``job`` in place."""
    url = job.get("url")
    if not isinstance(url, str) or _job_url(url)[0] is None:
        log.warning(f"{BOARD.name}: not a job page: {url!r}")
        return ""
    soup = _page(get, url)
    if soup is None:
        return ""
    try:
        return _enrich(soup, job)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        log.warning(f"{BOARD.name}: could not read {url}: {error}")
        return ""
