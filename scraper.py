#!/usr/bin/env python3
"""Job scraper for UAE/Saudi Arabia markets with Telegram notifications."""

import hashlib
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Config ───────────────────────────────────────────────────────────────────

CONFIG = {
    "keywords": [
        "software architect",
        "cloud architect",
        "tech lead",
        "lead software engineer",
        "platform architect",
        "solutions architect",
    ],
    "locations": [
        "UAE",
        "Saudi Arabia",
        "Dubai",
        "Riyadh",
    ],
    "scoring": {
        "high": {
            "weight": 3,
            "terms": [
                "architect", "aws", "azure",
                "spring boot", "microservices", "tech lead", "team lead", "java", "kotlin", "backend",
            ],
        },
        "medium": {
            "weight": 1,
            "terms": [
                "docker", "ci/cd", "cicd", "kubernetes", "terraform",
                "cloud", ".net", "typescript", "devops", "infrastructure",
            ],
        },
    },
    "exclude_terms": [
        "test engineer", "qa engineer", "quality assurance",
        "staff software engineer", "manual test", "sdet",
        "machine learning", "ml engineer", "ml architect",
    ],
    "tech_terms": [
        "python", "java", "kotlin", "javascript", "typescript", "go", "golang",
        "rust", "c#", ".net", "node.js", "react", "angular", "vue",
        "spring boot", "spring", "django", "flask", "fastapi",
        "aws", "azure", "gcp", "google cloud",
        "kubernetes", "k8s", "docker", "terraform", "ansible", "pulumi",
        "jenkins", "gitlab ci", "github actions", "ci/cd",
        "microservices", "rest", "graphql", "grpc", "kafka", "rabbitmq",
        "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
        "linux", "nginx", "devops", "sre", "observability", "prometheus", "grafana",
        "agile", "scrum", "jira",
    ],
    "local_presence_phrases": [
        "must be based in", "must be located in", "must be residing",
        "must currently reside", "must already be", "candidates must be in",
        "locally based", "local candidates only", "candidates already in",
        "currently based in", "currently living in", "currently residing in",
        "based in the uae", "based in dubai", "based in saudi",
        "residents only", "uae residents only", "saudi residents only",
        "no relocation", "no visa sponsorship", "not offering visa",
        "will not sponsor", "won't sponsor", "does not sponsor",
        "valid uae residence", "valid residence visa", "existing visa",
    ],
    "max_experience": 8,
    "max_job_age_days": 7,
    "score_threshold": 9,
    "min_matching_jobs": 3,
    "rate_limit": {"min": 2, "max": 5},
    "db_path": "./data/jobs.db",
    "log_path": "./data/scraper.log",
    "max_pages": 10,
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
]

# ── Logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger("scraper")


def setup_logging():
    log_path = Path(CONFIG["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)

    log.setLevel(logging.INFO)
    log.addHandler(file_handler)
    log.addHandler(stderr_handler)


# ── Database ─────────────────────────────────────────────────────────────────


def init_db():
    db_path = Path(CONFIG["db_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            source TEXT,
            score INTEGER,
            date_posted TEXT,
            date_scraped TEXT,
            notified INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            tech_required TEXT DEFAULT '',
            tech_nice_to_have TEXT DEFAULT '',
            min_experience INTEGER DEFAULT -1,
            salary TEXT DEFAULT '',
            work_model TEXT DEFAULT 'on-site',
            score_breakdown TEXT DEFAULT ''
        )
    """)
    conn.commit()
    return conn


def is_job_seen(conn, job_id):
    row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row is not None


def save_job(conn, job):
    conn.execute(
        """INSERT OR IGNORE INTO jobs
           (id, title, company, location, url, source, score, date_posted, date_scraped, notified, description, tech_required, tech_nice_to_have, min_experience, salary, work_model, score_breakdown)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["id"],
            job["title"],
            job["company"],
            job["location"],
            job["url"],
            job["source"],
            job["score"],
            job.get("date_posted", ""),
            datetime.now(timezone.utc).isoformat(),
            0,
            job.get("description", ""),
            job.get("tech_required", ""),
            job.get("tech_nice_to_have", ""),
            job.get("min_experience", -1),
            job.get("salary", ""),
            job.get("work_model", "on-site"),
            job.get("score_breakdown", ""),
        ),
    )
    conn.commit()


def mark_notified(conn, job_ids):
    conn.executemany(
        "UPDATE jobs SET notified = 1 WHERE id = ?", [(jid,) for jid in job_ids]
    )
    conn.commit()


# ── HTTP Session ─────────────────────────────────────────────────────────────


def create_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def rate_limited_get(session, url, **kwargs):
    delay = random.uniform(CONFIG["rate_limit"]["min"], CONFIG["rate_limit"]["max"])
    time.sleep(delay)
    session.headers["User-Agent"] = random.choice(USER_AGENTS)
    return session.get(url, timeout=30, **kwargs)


# ── LinkedIn Scraper ─────────────────────────────────────────────────────────


def scrape_linkedin(session, keyword, location):
    jobs = []
    base_url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    for page in range(CONFIG["max_pages"]):
        start = page * 25
        params = {
            "keywords": keyword,
            "location": location,
            "start": start,
        }

        log.info(f"LinkedIn: '{keyword}' in '{location}' page {page + 1}")

        try:
            resp = rate_limited_get(session, base_url, params=params)
        except requests.RequestException as e:
            log.warning(f"LinkedIn request failed: {e}")
            break

        if resp.status_code == 429:
            log.warning("LinkedIn rate limited (429), stopping pagination")
            break
        if resp.status_code != 200:
            log.warning(f"LinkedIn returned {resp.status_code}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.find_all("div", class_="base-search-card")

        if not cards:
            log.info("LinkedIn: no more results")
            break

        for card in cards:
            try:
                title_el = card.find("span", class_="sr-only")
                company_el = card.find("h4", class_="base-search-card__subtitle")
                location_el = card.find("span", class_="job-search-card__location")
                link_el = card.find("a", class_="base-card__full-link")
                date_el = card.find("time", class_="job-search-card__listdate")

                if not title_el or not link_el:
                    continue

                url = link_el["href"].split("?")[0]
                # Extract LinkedIn job ID from URL
                job_id_match = url.rstrip("/").split("-")[-1]
                job_id = f"li-{job_id_match}" if job_id_match.isdigit() else f"li-{hashlib.md5(url.encode()).hexdigest()[:12]}"

                jobs.append({
                    "id": job_id,
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else location,
                    "url": url,
                    "source": "LinkedIn",
                    "date_posted": date_el["datetime"] if date_el and date_el.has_attr("datetime") else "",
                })
            except (KeyError, AttributeError) as e:
                log.debug(f"LinkedIn: skipping card: {e}")
                continue

        log.info(f"LinkedIn: found {len(cards)} cards on page {page + 1}")

    return jobs


# ── Bayt Scraper ─────────────────────────────────────────────────────────────

BAYT_LOCATION_MAP = {
    "UAE": "uae",
    "Dubai": "uae",
    "Saudi Arabia": "saudi-arabia",
    "Riyadh": "saudi-arabia",
}


def scrape_bayt(session, keyword, location):
    jobs = []
    location_slug = BAYT_LOCATION_MAP.get(location, location.lower().replace(" ", "-"))
    keyword_slug = keyword.replace(" ", "-")

    for page in range(1, CONFIG["max_pages"] + 1):
        url = f"https://www.bayt.com/en/{location_slug}/jobs/{keyword_slug}-jobs/?page={page}"

        log.info(f"Bayt: '{keyword}' in '{location}' page {page}")

        try:
            resp = rate_limited_get(session, url)
        except requests.RequestException as e:
            log.warning(f"Bayt request failed: {e}")
            break

        if resp.status_code == 403:
            log.warning("Bayt returned 403, stopping")
            break
        if resp.status_code != 200:
            log.warning(f"Bayt returned {resp.status_code}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        listings = soup.find_all("li", attrs={"data-js-job": True})

        if not listings:
            log.info("Bayt: no more results")
            break

        for listing in listings:
            try:
                title_link = listing.find("h2")
                if not title_link:
                    continue
                a_tag = title_link.find("a")
                if not a_tag:
                    continue

                title = a_tag.get_text(strip=True)
                href = a_tag.get("href", "")
                job_url = f"https://www.bayt.com{href}" if href.startswith("/") else href

                company_el = listing.find("div", class_="is-company")
                location_el = listing.find("div", class_="is-location")

                job_id = f"bayt-{hashlib.md5(job_url.encode()).hexdigest()[:12]}"

                jobs.append({
                    "id": job_id,
                    "title": title,
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else location,
                    "url": job_url,
                    "source": "Bayt",
                    "date_posted": "",
                })
            except (KeyError, AttributeError) as e:
                log.debug(f"Bayt: skipping listing: {e}")
                continue

        log.info(f"Bayt: found {len(listings)} listings on page {page}")

    return jobs


# ── Job Details ──────────────────────────────────────────────────────────────


def fetch_job_description(session, job):
    """Fetch the full job description from the job detail page."""
    try:
        resp = rate_limited_get(session, job["url"])
        if resp.status_code != 200:
            log.debug(f"Could not fetch details for {job['url']}: {resp.status_code}")
            return ""
        soup = BeautifulSoup(resp.text, "lxml")

        # LinkedIn detail page
        if job["source"] == "LinkedIn":
            desc_el = soup.find("div", class_="show-more-less-html__markup")
            if desc_el:
                return desc_el.get_text(separator=" ", strip=True)

        # Bayt detail page
        if job["source"] == "Bayt":
            desc_el = soup.find("div", class_="is-rich-text")
            if desc_el:
                return desc_el.get_text(separator=" ", strip=True)

        return ""
    except requests.RequestException as e:
        log.debug(f"Failed to fetch job details: {e}")
        return ""


REQUIRED_HEADERS = re.compile(
    r'(?:requirements?|required|must[\s-]?have|qualifications?|key skills|what you.?ll need|'
    r'what we.?re looking for|essential|minimum qualifications?|responsibilities)',
    re.IGNORECASE,
)
NICE_TO_HAVE_HEADERS = re.compile(
    r'(?:nice[\s-]?to[\s-]?have|preferred|bonus|plus|desired|good[\s-]?to[\s-]?have|'
    r'advantageous|ideally|optional|additional skills|it.?s a plus|would be a plus|'
    r'preferred qualifications?|not required but)',
    re.IGNORECASE,
)


def _find_tech_in_text(text):
    """Find all tech terms present in a text block."""
    text_lower = text.lower()
    return [term for term in CONFIG["tech_terms"] if term.lower() in text_lower]


def extract_tech_keywords(text):
    """Extract tech keywords split into required and nice-to-have based on description sections."""
    if not text:
        return [], []

    # Split description into chunks by common section boundaries
    # We look for lines that match known headers
    lines = text.split("\n")
    if len(lines) <= 1:
        # Single block of text — try splitting on sentences with header keywords
        lines = re.split(r'(?<=[.:])\s+', text)

    required = []
    nice_to_have = []
    current_section = "required"  # default: assume required until we see a nice-to-have header

    for line in lines:
        # Check if this line is a section header
        if NICE_TO_HAVE_HEADERS.search(line):
            current_section = "nice"
        elif REQUIRED_HEADERS.search(line):
            current_section = "required"

        found = _find_tech_in_text(line)
        for term in found:
            if current_section == "nice":
                if term not in nice_to_have:
                    nice_to_have.append(term)
            else:
                if term not in required:
                    required.append(term)

    # If a term appears in both, keep it only in required
    nice_to_have = [t for t in nice_to_have if t not in required]

    return required, nice_to_have


def extract_min_experience(text):
    """Extract minimum years of experience from job description. Returns -1 if not found."""
    patterns = [
        r'(\d+)\+?\s*(?:years?|yrs?)[\s\w]*(?:of\s+)?(?:experience|exp)',
        r'(?:minimum|min|at\s+least|over)\s+(\d+)\s*(?:years?|yrs?)',
        r'(\d+)\s*(?:-|to)\s*\d+\s*(?:years?|yrs?)[\s\w]*(?:of\s+)?(?:experience|exp)',
        r'(?:experience|exp)\s*(?:of\s+)?(\d+)\+?\s*(?:years?|yrs?)',
    ]
    years_found = []
    text_lower = text.lower()
    for pattern in patterns:
        matches = re.findall(pattern, text_lower)
        for m in matches:
            val = int(m)
            if 1 <= val <= 30:
                years_found.append(val)
    return min(years_found) if years_found else -1


def extract_salary(text):
    """Extract salary information from job description. Returns empty string if not found."""
    patterns = [
        # Currency then amount: "AED 25,000 - 40,000" or "$90k - $120k" (min 4 digits or k suffix)
        r'(?:aed|usd|sar|us\$|\$|£|€)\s*(\d{1,3}(?:,\d{3})+|\d{4,}|\d+[kK])\s*(?:[-–to]+\s*(?:aed|usd|sar|us\$|\$|£|€)?\s*(?:\d{1,3}(?:,\d{3})+|\d{4,}|\d+[kK]))?\s*(?:per\s+(?:month|year|annum)|p\.?[am]\.?|monthly|annually|\/\s*(?:month|year|mo|yr))?',
        # Amount then currency: "25,000 - 40,000 AED"
        r'(\d{1,3}(?:,\d{3})+|\d{4,}|\d+[kK])\s*(?:[-–to]+\s*(?:\d{1,3}(?:,\d{3})+|\d{4,}|\d+[kK])\s*)?(?:aed|usd|sar|us\$|\$|£|€)\s*(?:per\s+(?:month|year|annum)|monthly|annually)?',
        # "salary: AED 25,000" or "compensation: 25,000 - 40,000"
        r'(?:salary|compensation|pay|package)\s*(?:range)?[\s:]+(?:aed|usd|sar|us\$|\$|£|€)?\s*(\d{1,3}(?:,\d{3})+|\d{4,}|\d+[kK])\s*(?:[-–to]+\s*(?:aed|usd|sar|us\$|\$|£|€)?\s*(?:\d{1,3}(?:,\d{3})+|\d{4,}|\d+[kK]))?',
    ]
    text_lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            return match.group(0).strip()
    return ""


def detect_work_model(text):
    """Detect if the job is remote, hybrid, or on-site."""
    text_lower = text.lower()

    remote_signals = [
        "fully remote", "100% remote", "work from home", "work from anywhere",
        "remote position", "remote role", "remote opportunity", "remote work",
        "work remotely",
    ]
    hybrid_signals = [
        "hybrid", "flexible work", "mix of remote", "partial remote",
        "days in office", "days remote", "work from office and home",
    ]

    is_remote = any(s in text_lower for s in remote_signals)
    is_hybrid = any(s in text_lower for s in hybrid_signals)

    if is_remote and is_hybrid:
        return "hybrid"
    if is_remote:
        return "remote"
    if is_hybrid:
        return "hybrid"
    return "on-site"


def job_age(date_posted):
    """Return a human-readable age string from an ISO date string."""
    if not date_posted:
        return ""
    try:
        posted = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - posted
        days = delta.days
        if days == 0:
            return "today"
        if days == 1:
            return "1 day ago"
        if days < 7:
            return f"{days} days ago"
        weeks = days // 7
        if weeks == 1:
            return "1 week ago"
        return f"{weeks} weeks ago"
    except (ValueError, TypeError):
        return ""


def requires_local_presence(text):
    """Check if job requires candidate to already be in the country."""
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in CONFIG["local_presence_phrases"])


# ── Scoring ──────────────────────────────────────────────────────────────────


def is_excluded(job):
    title = job["title"].lower()
    return any(term in title for term in CONFIG["exclude_terms"])


def score_job(job):
    """Score a job and return (score, breakdown) where breakdown lists matched terms."""
    text = f"{job['title']} {job['company']} {job.get('description', '')}".lower()
    score = 0
    breakdown = []

    for tier_name, tier in CONFIG["scoring"].items():
        for term in tier["terms"]:
            if term.lower() in text:
                score += tier["weight"]
                breakdown.append(f"{term}(+{tier['weight']})")

    return score, breakdown


# ── Telegram ─────────────────────────────────────────────────────────────────


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def format_job_message(job):
    stars = "\u2b50" * min(job["score"] // 3, 5) if job["score"] >= 3 else ""
    score_line = f"Score: {job['score']} {stars}" if stars else f"Score: {job['score']}"

    # Location with country extracted
    loc_parts = [p.strip() for p in job["location"].split(",")]
    country = loc_parts[-1] if loc_parts else job["location"]

    # Work model badge
    wm = job.get("work_model", "on-site")
    wm_badge = {"remote": "\U0001f30d Remote", "hybrid": "\U0001f3e0 Hybrid"}.get(wm, "\U0001f3e2 On-site")

    # Experience
    exp = job.get("min_experience", -1)
    exp_line = f"\U0001f4c5 {exp}+ years required" if exp > 0 else ""

    # Salary
    sal = job.get("salary", "")
    sal_line = f"\U0001f4b0 {sal}" if sal else ""

    # Tech stacks
    req = job.get("tech_required", "")
    nice = job.get("tech_nice_to_have", "")
    tech_lines = ""
    if req:
        tech_lines += f"\n  \u2705 <b>Required:</b> {req}"
    if nice:
        tech_lines += f"\n  \U0001f7e1 <b>Nice to have:</b> {nice}"

    # Job age
    age = job_age(job.get("date_posted", ""))
    age_line = f"\U0001f552 Posted {age}" if age else ""

    # Score breakdown
    bd = job.get("score_breakdown", "")
    bd_line = f"\U0001f4ca {bd}" if bd else ""

    # Build message
    lines = [
        f"\u2022 <b>{job['title']}</b>",
        f"  {job['company']} \u2014 {country}",
        f"  {wm_badge} | {score_line}",
    ]
    if age_line:
        lines.append(f"  {age_line}")
    if exp_line:
        lines.append(f"  {exp_line}")
    if sal_line:
        lines.append(f"  {sal_line}")
    if bd_line:
        lines.append(f"  {bd_line}")
    if tech_lines:
        lines.append(f"  {tech_lines.strip()}")
    lines.append(f"  <a href=\"{job['url']}\">View Job</a> ({job['source']})")

    return "\n".join(lines)


def notify_new_jobs(token, chat_id, jobs):
    if not jobs:
        return

    sorted_jobs = sorted(jobs, key=lambda j: j["score"], reverse=True)

    header = f"<b>\U0001f4bc {len(sorted_jobs)} new matching job(s) found!</b>\n\n"
    messages = []
    current = header

    for job in sorted_jobs:
        entry = format_job_message(job) + "\n\n"
        if len(current) + len(entry) > 4000:
            messages.append(current.rstrip())
            current = entry
        else:
            current += entry

    if current.strip():
        messages.append(current.rstrip())

    for msg in messages:
        if send_telegram(token, chat_id, msg):
            log.info("Telegram message sent")
        else:
            log.warning("Failed to send Telegram message")
        time.sleep(1)  # respect rate limit


# ── Main ─────────────────────────────────────────────────────────────────────

SCRAPERS = [
    ("LinkedIn", scrape_linkedin),
    ("Bayt", scrape_bayt),
]


def main():
    load_dotenv()
    setup_logging()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    telegram_enabled = bool(token and chat_id)

    if not telegram_enabled:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — running without notifications")

    log.info("Starting job scraper")

    conn = init_db()
    session = create_session()
    new_jobs = []
    target = CONFIG["min_matching_jobs"]
    done = False

    for keyword in CONFIG["keywords"]:
        if done:
            break
        for location in CONFIG["locations"]:
            if done:
                break
            for scraper_name, scraper_fn in SCRAPERS:
                if done:
                    break
                try:
                    jobs = scraper_fn(session, keyword, location)
                    log.info(f"{scraper_name}: got {len(jobs)} results for '{keyword}' in '{location}'")

                    for job in jobs:
                        if is_job_seen(conn, job["id"]):
                            continue
                        if is_excluded(job):
                            log.debug(f"Excluded: {job['title']}")
                            continue

                        # Filter out old jobs
                        max_age = CONFIG["max_job_age_days"]
                        date_posted = job.get("date_posted", "")
                        if date_posted:
                            try:
                                posted = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
                                if posted.tzinfo is None:
                                    posted = posted.replace(tzinfo=timezone.utc)
                                age_days = (datetime.now(timezone.utc) - posted).days
                                if age_days > max_age:
                                    log.debug(f"Skipped (posted {age_days}d ago > {max_age}d max): {job['title']}")
                                    continue
                            except (ValueError, TypeError):
                                pass

                        # Fetch full job description
                        log.info(f"Fetching details: {job['title']}")
                        desc = fetch_job_description(session, job)
                        job["description"] = desc

                        # Filter out jobs requiring local presence
                        if requires_local_presence(desc):
                            log.info(f"Skipped (requires local presence): {job['title']} @ {job['company']}")
                            continue

                        req, nice = extract_tech_keywords(desc)
                        job["tech_required"] = ", ".join(req)
                        job["tech_nice_to_have"] = ", ".join(nice)
                        job["min_experience"] = extract_min_experience(desc)

                        # Filter out jobs requiring too many years
                        max_exp = CONFIG["max_experience"]
                        if job["min_experience"] > max_exp:
                            log.info(f"Skipped ({job['min_experience']}+ yrs > {max_exp} max): {job['title']} @ {job['company']}")
                            continue

                        job["salary"] = extract_salary(desc)
                        job["work_model"] = detect_work_model(desc)
                        score, breakdown = score_job(job)
                        job["score"] = score
                        job["score_breakdown"] = ", ".join(breakdown)
                        save_job(conn, job)

                        if job["score"] >= CONFIG["score_threshold"]:
                            new_jobs.append(job)
                            age = job_age(job.get("date_posted", ""))
                            age_str = f" | {age}" if age else ""
                            exp_str = f" | {job['min_experience']}+ yrs" if job["min_experience"] > 0 else ""
                            sal_str = f" | {job['salary']}" if job["salary"] else ""
                            wm_str = f" | {job['work_model']}" if job["work_model"] != "on-site" else ""
                            log.info(f"New match ({len(new_jobs)}/{target}): {job['title']} @ {job['company']} (score={job['score']}{age_str}{exp_str}{sal_str}{wm_str})")
                            log.info(f"        Score: {job['score_breakdown']}")

                            if len(new_jobs) >= target:
                                log.info(f"Reached target of {target} matching jobs, stopping")
                                done = True
                                break

                except Exception:
                    log.exception(f"{scraper_name} failed for '{keyword}' in '{location}'")

    if new_jobs:
        log.info(f"Found {len(new_jobs)} new matching job(s)")
        if telegram_enabled:
            notify_new_jobs(token, chat_id, new_jobs)
            mark_notified(conn, [j["id"] for j in new_jobs])
        else:
            log.info("Telegram disabled — printing results to console")
            for job in sorted(new_jobs, key=lambda j: j["score"], reverse=True):
                age = job_age(job.get("date_posted", ""))
                age_str = f" | Posted {age}" if age else ""
                exp = f" | Min {job['min_experience']}+ yrs" if job.get("min_experience", -1) > 0 else ""
                sal = f" | Salary: {job['salary']}" if job.get("salary") else ""
                wm = f" | {job['work_model'].upper()}" if job.get("work_model", "on-site") != "on-site" else ""
                log.info(f"  [{job['score']}] {job['title']} @ {job['company']} — {job['location']}{age_str}{exp}{sal}{wm} ({job['source']})")
                log.info(f"        Score: {job['score_breakdown']}")
                if job.get("tech_required"):
                    log.info(f"        Required: {job['tech_required']}")
                if job.get("tech_nice_to_have"):
                    log.info(f"        Nice to have: {job['tech_nice_to_have']}")
                log.info(f"        {job['url']}")
    else:
        log.info("No new matching jobs found")

    conn.close()
    log.info("Scraper run complete")


if __name__ == "__main__":
    main()
