"""SpainJobs.io: a curated board of English-friendly jobs in Spain.

Listing: ``GET https://www.spainjobs.io/api/jobs?city={slug}&category=engineering
&sort=newest&limit=48[&cursor=...]`` with ``Accept: application/json``. It is
the JSON the site's own "load more" button reads: newest first, 48 rows a
page at most, cursor paging, and per row the board's language, visa,
relocation and employer-stated salary fields. The board has no keyword search
worth using (its ``q`` mixes a loose "related" tier into the results), so each
city's engineering category is listed and titles are filtered locally.
robots.txt disallows ``/api/``; it is used anyway (about fifteen requests a
night, rate-limited) because the robots-allowed city pages show 12 rows in
"best" order and cannot page.

Detail: ``GET https://www.spainjobs.io/companies/{companySlug}/{titleSlug}``,
server-rendered. The employer's advert is ``div#job-advert``; the Apply anchor
links straight to the employer's ATS.

Deliberately not fetched or kept: ``/jobs/search?`` (robots-disallowed), the
board's net-pay estimate and its ``#job-overview`` facts ("X doesn't mention
sponsorship" would trip the sponsorship rules), and the board-written company
profile and "Good to know if you are moving" notes inside the advert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import re

from bs4 import BeautifulSoup
import requests

from . import base

log = logging.getLogger("scraper")

BOARD = base.Board(
    name="SpainJobs.io",
    key="sj",
    module=__name__,
    keyword_search=False,
    countries=frozenset({"es"}),
    hosts=frozenset({"www.spainjobs.io", "spainjobs.io"}),
    canonical_host="www.spainjobs.io",
    job_path=r"/companies/[a-z0-9-]+/(?:[a-z0-9-]+--)?([a-f0-9]{10})/?",
    # The header is the block holding the h1, the company link and the Apply
    # anchors (the desktop copy is class="hidden", the mobile copy visible).
    selectors={"header": "div:has(> div > h1)",
               "company": "a[href^='/companies/'] span[translate='no']",
               "description": "div#job-advert"},
    high_priority=True,
)

SITE = "https://www.spainjobs.io"
API = f"{SITE}/api/jobs"
PAGE_SIZE = 48  # the server caps limit at 48
NOTICE = "This job was automatically translated to English."
# INE province codes: rows filed under a city but placed in a nearby town.
PROVINCES = {"Valencia": "46", "Madrid": "28", "Barcelona": "08"}
CURRENCY = {"EUR": "€", "USD": "$", "GBP": "£"}
INTERVAL = {"year": "a year", "month": "a month", "week": "a week", "day": "a day", "hour": "an hour"}


def _fold(text):
    return base.strip_accents(text).lower()


def display_location(raw, province_codes, city):
    """A location ``job_scoring.location_allowed`` accepts for a row listed under ``city``.

    "Las Rozas de Madrid, Spain / ... / València, Spain" keeps the segment that
    names the city (two chosen cities in one string are rejected downstream);
    "Cortes Valencianas, Spain" in province 46 becomes "Cortes Valencianas
    (province of Valencia), Spain"; a list naming no chosen city but coded
    for the province becomes "Province of Valencia, Spain"; anything else goes through
    ``base.spanish_location``, which keeps foreign places for rejection.
    """
    raw = " ".join(str(raw or "").split())
    segments = [s.strip() for s in re.split(r"\s+/\s+", raw) if s.strip()]
    if not segments:
        return base.spanish_location(raw, city)
    terms = {_fold(name) for name, canonical in base.SPANISH_CITIES.items() if canonical == city}
    word = re.compile(r"(?<![a-z])(?:%s)(?![a-z])" % "|".join(map(re.escape, sorted(terms))))
    exact = [s for s in segments if _fold(re.split(r"[,|(]", s)[0]).strip() in terms]
    loose = [s for s in segments if word.search(_fold(s))]
    for segment in exact + loose:
        segment = re.sub(r"\s*\|\s*", ", ", segment)
        return segment if re.search(r"spain|espana", _fold(segment)) else f"{segment}, Spain"
    if len(segments) > 1:
        # A list that names another chosen city outright ("València / Getxo /
        # Tres Cantos" under Madrid) is a job in that city.
        other = next((s for s in segments if base.city_of(s)), None)
        if other is not None:
            return base.spanish_location(other, city)
    if isinstance(province_codes, (list, tuple)) and PROVINCES.get(city) in province_codes:
        if len(segments) > 1:
            # The code says one of the places is in the province, not which.
            return f"Province of {city}, Spain"
        match = re.match(r"^(.*?)(?:,\s*(?:spain|españa))?\s*(\([^)]*\))?\s*$", segments[0], re.I)
        town, paren = (match.group(1), match.group(2)) if match else (segments[0], None)
        if re.search(r"community|comunidad|comunitat|provinc|region", _fold(town)):
            # A region, not a town: nothing places it in the chosen city.
            return base.spanish_location(raw, city)
        return f"{town} (province of {city}), Spain" + (f" {paren}" if paren else "")
    return base.spanish_location(raw, city)


def language_requirement(row):
    """What the board says about languages, following its own "No Spanish needed" rule."""
    spanish, ad_language = row.get("spanishRequirement"), row.get("language")
    english = row.get("isEnglishRequired") is True
    if spanish in ("fluent", "native"):
        parts = [f"{spanish} Spanish required"]
    elif spanish == "none" and (ad_language == "en" or english):
        parts = ["no Spanish needed (ad in English)" if ad_language == "en" else "no Spanish needed (English required)"]
    elif ad_language in ("es", "en"):
        written = "ad written in Spanish" if ad_language == "es" else "ad in English"
        parts = [f"{written}, Spanish requirement not stated"]
    else:
        parts = []
    others = row.get("languageRequirements")
    parts += [f"{str(lang).capitalize()} required" for lang in (others if isinstance(others, list) else [])
              if str(lang).strip() and str(lang).lower() not in ("spanish", "english")]
    return "board: " + "; ".join(parts) if parts else ""


def _amount(value, symbol):
    number = float(value)
    text = f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"
    return f"{symbol}{text}" if len(symbol) == 1 else f"{symbol} {text}"


def salary_text(row):
    """The employer-stated pay the board copied from the ad, or ""."""
    low, high = row.get("salaryMin"), row.get("salaryMax")
    try:
        symbol = CURRENCY.get(row.get("salaryCurrency") or "", str(row.get("salaryCurrency") or "").strip())
        if low is None and high is None or not symbol:
            return ""
        if low is None or high is None or low == high:
            span = _amount(low if high is None else high, symbol)
            span = f"from {span}" if high is None else f"up to {span}" if low is None else span
        else:
            span = f"{_amount(low, symbol)} - {_amount(high, symbol)}"
    except (TypeError, ValueError):
        return ""
    interval = INTERVAL.get(row.get("salaryInterval") or "")
    return f"{span} {interval}" if interval else span


def board_facts(row):
    labels = (("visaSponsorshipStatus", "visa sponsorship"), ("workAuthorizationRoute", "work authorization route"),
              ("workAuthorizationEvidenceLabel", "work authorization evidence"),
              ("relocationSupport", "relocation"), ("remotePolicy", "remote policy"), ("seniorityLevel", "seniority"))
    return "; ".join(f"{label}: {row[key]}" for key, label in labels if row.get(key) not in (None, ""))


def _card(row, city):
    title, company = " ".join(str(row.get("title") or "").split()), " ".join(str(row.get("company") or "").split())
    company_slug, title_slug = str(row.get("companySlug") or ""), str(row.get("titleSlug") or "")
    path = f"/companies/{company_slug}/{title_slug}"
    match = re.fullmatch(BOARD.job_path, path)
    if not (title and company_slug and match):
        return None
    job = {
        "id": base.stable_id(BOARD, match.group(1)),
        "title": title,
        "company": company or "Unknown",
        "location": display_location(row.get("location"), row.get("provinceCodes"), city),
        "location_raw": row.get("location") or "",
        "url": f"{SITE}{path}",
        "source": BOARD.name,
        "date_posted": base.iso_date(row.get("postedAt")) if row.get("postedAt") else "",
        "language_requirement": language_requirement(row),
        "board_facts": board_facts(row),
    }
    pay = salary_text(row)
    if pay:
        job["salary_text"] = pay
    return job


def _older_than(value, cutoff):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) < cutoff
    except (TypeError, ValueError):
        return False


def scrape(session, keyword, location, *, get, config):
    """One list of job dicts per page of the city's newest engineering rows.

    ``keyword`` is None (keyword-less board). Stops on the last page, an HTTP
    or body error, ``max_pages``, or once a page reaches rows older than
    ``max_job_age_days``: the list is newest first, so the next page would be
    older still.
    """
    city = base.city_of(location)
    if city is None:
        log.info(f"SpainJobs.io: no city slug for {location!r}, skipped")
        return
    max_age = config.get("max_job_age_days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age) if max_age else None
    params = {"city": base.city_slug(location), "category": "engineering", "sort": "newest", "limit": PAGE_SIZE}
    headers = {"Accept": "application/json", "Referer": f"{SITE}/jobs/{params['city']}"}
    seen = set()
    for page in range(int(config.get("max_pages") or 10)):
        log.info(f"SpainJobs.io: engineering in '{location}' page {page + 1}")
        try:
            resp = get(API, params=params, headers=headers)
        except requests.RequestException as error:
            log.warning(f"SpainJobs.io request failed: {error}")
            return
        if resp.status_code != 200:
            log.warning(f"SpainJobs.io returned {resp.status_code}")
            return
        try:
            data = resp.json()
            rows = [row for row in data.get("jobs") or [] if isinstance(row, dict)]
        except (ValueError, AttributeError, TypeError):
            log.warning("SpainJobs.io returned a body that is not the jobs JSON")
            return
        if not rows:
            log.info("SpainJobs.io: no more results")
            return
        old = [cutoff is not None and _older_than(row.get("postedAt"), cutoff) for row in rows]
        if all(old):
            log.info(f"SpainJobs.io: page {page + 1} is older than {max_age} days, stopping")
            return
        jobs = []
        for row in rows:
            if str(row.get("roleFamily") or "") == "hardware-industrial":
                # Mechanical, civil and process engineers share the category;
                # the rubric drops every one of them after a 370 KB fetch.
                continue
            if not base.title_matches_search(row.get("title"), config):
                continue
            job = _card(row, city)
            if job and job["id"] not in seen:
                seen.add(job["id"])
                jobs.append(job)
        log.info(f"SpainJobs.io: kept {len(jobs)} of {len(rows)} rows on page {page + 1}")
        yield jobs
        cursor = data.get("nextCursor")
        if not cursor or any(old):
            return
        params = {**params, "cursor": cursor}


def _rsc_text(html):
    """The page's React server payload: the joined ``rsc.push("...")`` strings."""
    chunks = []
    for chunk in re.findall(r'\.rsc\.push\((".*?")\)</script>', html, re.S):
        try:
            chunks.append(json.loads(chunk))
        except ValueError:
            continue
    return "".join(chunks)


def _overview_language(soup):
    """A language note from the board's overview lines, for jobs without a card."""
    overview = soup.select_one("div#job-overview")
    lines = [" ".join(node.get_text(" ", strip=True).split()) for node in overview.find_all("p")] if overview else []
    spanish = next((m.group(1) for line in lines if (m := re.fullmatch(r"Speak (fluent|native) Spanish", line, re.I))), None)
    if spanish:
        return f"board: {spanish.lower()} Spanish required"
    if any(re.fullmatch(r"Work in English", line, re.I) for line in lines):
        return "board: no Spanish needed (English required)"
    return ""


def _advert(soup):
    """The employer's advert without the board's own notes and links."""
    advert = soup.select_one("div#job-advert")
    if advert is None:
        return None, False
    for node in advert.select("div#job-overview, #job-location, [id^='job-row-'], dl, a[href^='/']"):
        node.decompose()
    company = advert.select_one("#job-company")
    if company is not None:
        for heading in company.find_all(["h3", "h4"]):
            if _fold(heading.get_text(" ", strip=True)).startswith("good to know") and heading.parent is not None:
                heading.parent.decompose()
        # Paragraphs outside a section are the board's own company profile;
        # the employer's words sit in the "In their own words" section.
        for paragraph in company.find_all("p"):
            if not any(company in section.parents for section in paragraph.find_parents("section")):
                paragraph.decompose()
    text = base.clean_text(advert)
    translated = NOTICE in text
    lines = [line for line in text.split("\n") if line.strip() != NOTICE]
    return "\n".join(lines).strip(), translated


def fetch_description(session, job, *, get):
    """The advert as plain text; sets apply_url, translated and missing enrichment fields."""
    try:
        resp = get(job["url"])
    except requests.RequestException as error:
        log.debug(f"SpainJobs.io detail failed for {job.get('url')}: {error}")
        return ""
    if resp.status_code != 200:
        log.debug(f"SpainJobs.io detail returned {resp.status_code} for {job.get('url')}")
        return ""
    html = resp.text or ""
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select("a[target='_blank'][href]"):
        href = anchor["href"].strip()
        if anchor.find_parent(["aside", "article"]) is not None:
            continue  # another job's card
        if anchor.get_text(" ", strip=True) == "Apply" and href.startswith("http") and "spainjobs.io" not in href:
            job["apply_url"] = href
            break
    else:
        match = re.search(r'"sourceUrl":"(https?://[^"]+)"', _rsc_text(html))
        if match and "spainjobs.io" not in match.group(1):
            job["apply_url"] = match.group(1)
    for dt in soup.select("#job-company dt"):
        link = dt.find_next_sibling("dd")
        link = link.find("a", href=True) if link is not None else None
        if dt.get_text(strip=True).casefold() == "website" and link and link["href"].startswith("http") \
                and not job.get("company_website"):
            job["company_website"] = link["href"].strip()
    postings = base.json_ld_job_postings(soup)
    if not job.get("date_posted") and postings:
        job["date_posted"] = base.iso_date(postings[0].get("datePosted"))
    if not job.get("language_requirement"):
        job["language_requirement"] = _overview_language(soup)
    text, translated = _advert(soup)
    if text is None:
        body = postings[0].get("description") if postings else None
        text = base.clean_text(body) if isinstance(body, str) else ""
    else:
        job["translated"] = translated
    if text and job.get("salary_text"):
        text += f"\n\nSalary: {job['salary_text']} (as stated in the ad)"
    return text
