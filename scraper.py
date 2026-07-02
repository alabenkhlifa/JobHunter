#!/usr/bin/env python3
"""Job scraper for Dubai market with Telegram notifications."""

import argparse
import hashlib
import json
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
        "senior software engineer",
        "senior backend engineer",
        "platform architect",
        "solutions architect",
    ],
    "regions": {
        "Dubai": ["Dubai"],
    },
    "allowed_locations": ["dubai"],
    "scoring": {
        "high": {
            "weight": 3,
            "terms": [
                "architect", "aws", "azure",
                "spring boot", "microservices", "tech lead", "team lead", "senior engineer", "senior software engineer", "senior backend engineer", "java", "kotlin", "backend",
            ],
        },
        "medium": {
            "weight": 1,
            "terms": [
                "docker", "ci/cd", "cicd", "kubernetes", "terraform",
                "cloud", ".net", "typescript",
            ],
        },
    },
    "penalty_terms": {
        "weight": -3,
        "terms": [
            "junior", "intern", "entry level", "entry-level",
            "graduate", "fresh graduate", "trainee",
        ],
    },
    "exclude_terms": [
        "test engineer", "qa engineer", "quality assurance",
        "staff software engineer", "manual test", "sdet",
        "machine learning", "ml engineer", "ml architect",
        # Infra / Network / SRE / Ops
        "infrastructure engineer", "infrastructure manager",
        "network engineer", "network architect", "network admin",
        "site reliability", "sre engineer",
        "sysadmin", "system administrator", "systems administrator",
        "systems engineer", "platform engineer",
        "devops engineer", "devops lead",
        # Specialized engineering
        "data engineer", "data architect", "data platform",
        "security engineer", "security architect", "cybersecurity",
        "information security", "infosec",
        "embedded engineer", "embedded software", "firmware",
        "hardware engineer", "hardware architect",
        # Frontend / UI roles
        "frontend engineer", "frontend developer", "front-end engineer", "front-end developer",
        "javascript developer", "typescript developer",
        "react developer", "react engineer", "react native",
        "angular developer", "angular engineer",
        "vue developer", "vue engineer",
        "ui developer", "ui engineer", "ux engineer",
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
    "score_threshold": 15,
    # Per scraper/region bucket. Keep this high so one good match does not stop
    # the scrape early; the LLM review can rank/reject multiple good offers.
    "min_matching_jobs": 25,
    "rate_limit": {"min": 2, "max": 5},
    "db_path": "./data/jobs.db",
    "log_path": "./data/scraper.log",
    "max_pages": 10,
}

DEFAULT_CONFIG = dict(CONFIG)


def load_profile_config(profile_name):
    """Load profile-specific config from data/<name>/config.json, merging with defaults."""
    if profile_name is None:
        return dict(DEFAULT_CONFIG)

    config_path = Path(f"data/{profile_name}/config.json")
    if not config_path.exists():
        print(f"Profile config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        profile_config = json.load(f)

    merged = dict(DEFAULT_CONFIG)
    merged.update(profile_config)
    merged["db_path"] = f"./data/{profile_name}/jobs.db"
    merged["log_path"] = f"./data/{profile_name}/scraper.log"
    return merged


# ── ntfy Notifications ───────────────────────────────────────────────────────


def send_ntfy(topic, title, message, tags=None):
    """Send a notification via ntfy.sh."""
    headers = {"Title": title}
    if tags:
        headers["Tags"] = ",".join(tags) if isinstance(tags, list) else tags
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning(f"ntfy returned {resp.status_code}: {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        log.warning(f"ntfy send failed: {e}")
        return False


def send_ntfy_file(topic, file_path, filename=None):
    """Send a file via ntfy.sh PUT upload."""
    file_path = Path(file_path)
    fname = filename or file_path.name
    try:
        with open(file_path, "rb") as f:
            resp = requests.put(
                f"https://ntfy.sh/{topic}",
                data=f,
                headers={"Filename": fname},
                timeout=30,
            )
        if resp.status_code != 200:
            log.warning(f"ntfy file upload returned {resp.status_code}: {resp.text}")
            return False
        return True
    except (requests.RequestException, OSError) as e:
        log.warning(f"ntfy file upload failed: {e}")
        return False


def format_job_message_plain(job):
    """Format a job as plain text for ntfy (includes job ID for reply flow)."""
    score = job["score"]
    if score >= 18:
        tier = "HOT MATCH"
    elif score >= 15:
        tier = "STRONG MATCH"
    else:
        tier = "GOOD MATCH"

    wm = job.get("work_model", "on-site")
    exp = job.get("min_experience", -1)
    sal = job.get("salary", "")
    age = job_age(job.get("date_posted", ""))
    req = job.get("tech_required", "")
    nice = job.get("tech_nice_to_have", "")
    bd = job.get("score_breakdown", "")

    lines = [
        f"{tier} (Score: {score})",
        "",
        f"Job: {job['title']}",
        f"Company: {job['company']}",
        f"Location: {job['location']}",
        f"Source: {job['source']}",
    ]
    if wm != "on-site":
        lines.append(f"Work: {wm.capitalize()}")
    if age:
        lines.append(f"Posted: {age}")
    if exp > 0:
        lines.append(f"Experience: {exp}+ years")
    if sal:
        lines.append(f"Salary: {sal}")
    if bd:
        lines.append(f"Score: {bd}")
    if req:
        lines.append(f"\nRequired: {req}")
    if nice:
        lines.append(f"Nice to have: {nice}")
    lines.extend(["", job["url"], "", f"Reply with: {job['id']}"])
    return "\n".join(lines)


def notify_new_jobs_ntfy(topic, jobs):
    """Send job notifications via ntfy."""
    if not jobs:
        return

    sorted_jobs = sorted(jobs, key=lambda j: j["score"], reverse=True)
    send_ntfy(topic, "Job Hunter", f"{len(sorted_jobs)} new matching job(s) found!", tags=["briefcase"])
    time.sleep(1)

    for job in sorted_jobs:
        msg = format_job_message_plain(job)
        title = f"{job['title']} @ {job['company']}"
        if send_ntfy(topic, title, msg, tags=["mag"]):
            log.info(f"Sent (ntfy): {job['title']} @ {job['company']}")
        else:
            log.warning(f"Failed to send (ntfy): {job['title']}")
        time.sleep(1)


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
            score_breakdown TEXT DEFAULT '',
            status TEXT DEFAULT 'new'
        )
    """)
    # Migration for existing databases
    try:
        conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
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


def mark_interested(conn, job_id):
    conn.execute("UPDATE jobs SET status = 'interested' WHERE id = ?", (job_id,))
    conn.commit()


def get_job_by_id(conn, job_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return dict(row)


def _dict_counts(rows):
    """Convert two-column SQLite count rows into a stable dict."""
    return {str(key): int(count) for key, count in rows}


def get_job_status_summary(conn):
    """Return compact, traceable counters for managing the JobHunter backlog."""
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    unreviewed = conn.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE status = 'new' AND COALESCE(notified, 0) = 0
        """
    ).fetchone()[0]
    by_status = _dict_counts(
        conn.execute(
            """
            SELECT COALESCE(status, 'new') AS status, COUNT(*)
            FROM jobs
            GROUP BY COALESCE(status, 'new')
            ORDER BY status
            """
        ).fetchall()
    )
    by_source = _dict_counts(
        conn.execute(
            """
            SELECT COALESCE(source, 'unknown') AS source, COUNT(*)
            FROM jobs
            GROUP BY COALESCE(source, 'unknown')
            ORDER BY source
            """
        ).fetchall()
    )
    by_status_and_notified = _dict_counts(
        conn.execute(
            """
            SELECT COALESCE(status, 'new') || ':' ||
                   CASE WHEN COALESCE(notified, 0) = 1 THEN 'notified' ELSE 'unnotified' END,
                   COUNT(*)
            FROM jobs
            GROUP BY COALESCE(status, 'new'), COALESCE(notified, 0)
            ORDER BY COALESCE(status, 'new'), COALESCE(notified, 0)
            """
        ).fetchall()
    )
    return {
        "total": int(total),
        "unreviewed": int(unreviewed),
        "by_status": by_status,
        "by_source": by_source,
        "by_status_and_notified": by_status_and_notified,
    }


def archive_stale_unreviewed_jobs(conn, older_than_days, now=None, dry_run=False):
    """
    Mark stale, unnotified, still-new jobs as archived.

    This intentionally does not touch notified, interested, skipped, unavailable,
    or already archived jobs so user decisions and application history are safe.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=older_than_days)).isoformat()
    params = (cutoff,)
    where = """
        status = 'new'
        AND COALESCE(notified, 0) = 0
        AND COALESCE(date_scraped, '') < ?
    """
    count = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", params).fetchone()[0]
    if not dry_run and count:
        conn.execute(f"UPDATE jobs SET status = 'archived' WHERE {where}", params)
        conn.commit()
    return int(count)

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


def normalize_location(location):
    """Return a display string for source-specific location values."""
    if isinstance(location, list):
        parts = []
        for item in location:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("name") or item.get("city") or "")
            else:
                parts.append(str(item))
        return ", ".join(p for p in parts if p)
    if isinstance(location, dict):
        return location.get("text") or location.get("name") or location.get("city") or str(location)
    return str(location or "")


def is_allowed_location(job):
    """Allow only jobs whose displayed location matches configured cities."""
    allowed = [loc.lower() for loc in CONFIG.get("allowed_locations", [])]
    if not allowed:
        return True
    location = normalize_location(job.get("location", "")).lower()
    return any(loc in location for loc in allowed)


# ── LinkedIn Scraper ─────────────────────────────────────────────────────────


def scrape_linkedin(session, keyword, location):
    """Generator that yields one page of jobs at a time (list per page)."""
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
            return

        if resp.status_code == 429:
            log.warning("LinkedIn rate limited (429), stopping pagination")
            return
        if resp.status_code != 200:
            log.warning(f"LinkedIn returned {resp.status_code}")
            return

        soup = BeautifulSoup(resp.text, "lxml")
        cards = soup.find_all("div", class_="base-search-card")

        if not cards:
            log.info("LinkedIn: no more results")
            return

        page_jobs = []
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
                job_id_match = url.rstrip("/").split("-")[-1]
                job_id = f"li-{job_id_match}" if job_id_match.isdigit() else f"li-{hashlib.md5(url.encode()).hexdigest()[:12]}"

                page_jobs.append({
                    "id": job_id,
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": normalize_location(location_el.get_text(strip=True) if location_el else location),
                    "url": url,
                    "source": "LinkedIn",
                    "date_posted": date_el["datetime"] if date_el and date_el.has_attr("datetime") else "",
                })
            except (KeyError, AttributeError) as e:
                log.debug(f"LinkedIn: skipping card: {e}")
                continue

        log.info(f"LinkedIn: found {len(page_jobs)} cards on page {page + 1}")
        yield page_jobs


# ── Foundit Gulf Scraper ─────────────────────────────────────────────────────

FOUNDIT_LOCATION_MAP = {
    "UAE": "United Arab Emirates",
    "Dubai": "Dubai, United Arab Emirates",
    "Saudi Arabia": "Saudi Arabia",
    "Riyadh": "Riyadh, Saudi Arabia",
}


def scrape_foundit(session, keyword, location):
    """Generator that yields one page of jobs at a time (list per page)."""
    seen_ids = set()
    location_param = FOUNDIT_LOCATION_MAP.get(location, location)
    max_exp = CONFIG["max_experience"]

    for page in range(CONFIG["max_pages"]):
        start = page * 15
        url = "https://www.founditgulf.com/middleware/jobsearch"
        params = {
            "query": keyword,
            "locations": location_param,
            "sort": 1,  # sort by date
            "limit": 15,
            "start": start,
            "experienceRanges": f"0~{max_exp}",
        }

        log.info(f"Foundit: '{keyword}' in '{location}' page {page + 1}")

        try:
            resp = rate_limited_get(
                session, url, params=params,
                headers={"Accept": "application/json", "Referer": "https://www.founditgulf.com/"},
            )
        except requests.RequestException as e:
            log.warning(f"Foundit request failed: {e}")
            return

        if resp.status_code != 200:
            log.warning(f"Foundit returned {resp.status_code}")
            return

        try:
            data = resp.json()
        except ValueError:
            log.warning("Foundit returned non-JSON response")
            return

        api_jobs = data.get("jobSearchResponse", {}).get("data", [])
        if not api_jobs:
            log.info("Foundit: no more results")
            return

        page_jobs = []
        for j in api_jobs:
            try:
                title = j.get("title") or j.get("cleanedJobTitle")
                if not title:
                    continue

                job_id = f"foundit-{j['id']}"
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                jd_url = j.get("seoJdUrl") or j.get("jdUrl", "")
                job_url = f"https://www.founditgulf.com{jd_url}" if jd_url else ""

                date_posted = ""
                created = j.get("createdAt")
                if created and isinstance(created, (int, float)):
                    date_posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()

                page_jobs.append({
                    "id": job_id,
                    "title": title,
                    "company": j.get("companyName", "Unknown"),
                    "location": normalize_location(j.get("locations", location)),
                    "url": job_url,
                    "source": "Foundit",
                    "date_posted": date_posted,
                })
            except (KeyError, AttributeError) as e:
                log.debug(f"Foundit: skipping job: {e}")
                continue

        log.info(f"Foundit: found {len(page_jobs)} new jobs on page {page + 1}")
        if not page_jobs:
            log.info("Foundit: no new results, stopping pagination")
            return
        yield page_jobs


# ── Job Details ──────────────────────────────────────────────────────────────


def fetch_job_description(session, job):
    """Fetch the full job description from the job detail page."""
    try:
        if job["source"] == "Foundit":
            # Use Foundit's job detail API
            job_id = job["id"].replace("foundit-", "")
            url = f"https://www.founditgulf.com/middleware/jobdetail/{job_id}"
            resp = rate_limited_get(
                session, url,
                headers={"Accept": "application/json", "Referer": "https://www.founditgulf.com/"},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    jd = data.get("jobDetailResponse", {})
                    desc_html = jd.get("description", "")
                    # Convert HTML description to text
                    desc = BeautifulSoup(desc_html, "lxml").get_text(separator="\n", strip=True) if desc_html else ""
                    skills = jd.get("skills", [])
                    if isinstance(skills, list) and skills:
                        skill_text = ", ".join(s.get("text", "") for s in skills if isinstance(s, dict))
                        if skill_text:
                            desc += f"\nSkills: {skill_text}"
                    return desc
                except ValueError:
                    pass
            return ""

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
    """Score a job based on required skills and title, with a flat +1 for nice-to-have matches."""
    required_text = f"{job['title']} {job.get('tech_required', '')}".lower()
    nice_text = job.get("tech_nice_to_have", "").lower()
    title = job["title"].lower()
    score = 0
    breakdown = []

    for tier_name, tier in CONFIG["scoring"].items():
        for term in tier["terms"]:
            t = term.lower()
            if t in required_text:
                score += tier["weight"]
                breakdown.append(f"{term}(+{tier['weight']})")
            elif t in nice_text:
                score += 1
                breakdown.append(f"{term}(+1 nice)")

    # Apply penalty terms against title
    penalty = CONFIG["penalty_terms"]
    for term in penalty["terms"]:
        if term.lower() in title:
            score += penalty["weight"]
            breakdown.append(f"{term}({penalty['weight']})")

    return score, breakdown


# ── Telegram ─────────────────────────────────────────────────────────────────


def send_telegram(token, chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def send_telegram_document(token, chat_id, file_path, caption=None):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(url, data=data, files={"document": f}, timeout=30)
        if resp.status_code != 200:
            log.warning(f"Telegram sendDocument returned {resp.status_code}: {resp.text}")
            return False
        return True
    except (requests.RequestException, OSError) as e:
        log.warning(f"Telegram sendDocument failed: {e}")
        return False


def format_job_message(job):
    score = job["score"]

    # Tier label based on raw score (threshold=13)
    if score >= 18:
        tier_label = "\U0001f525 HOT MATCH"
    elif score >= 15:
        tier_label = "\u2b50 STRONG MATCH"
    else:
        tier_label = "\u2705 GOOD MATCH"

    # Work model
    wm = job.get("work_model", "on-site")

    # Experience
    exp = job.get("min_experience", -1)

    # Salary
    sal = job.get("salary", "")

    # Job age
    age = job_age(job.get("date_posted", ""))

    # Tech stacks
    req = job.get("tech_required", "")
    nice = job.get("tech_nice_to_have", "")

    # Score breakdown
    bd = job.get("score_breakdown", "")

    lines = [
        f"{tier_label} (Score: {score})",
        "",
        f"\U0001f4cb <b>{job['title']}</b>",
        f"\U0001f3e2 {job['company']}",
        f"\U0001f4cd {job['location']}",
        f"\U0001f4e1 Source: {job['source']}",
    ]

    if wm != "on-site":
        lines.append(f"\U0001f4bc {wm.capitalize()}")
    if age:
        lines.append(f"\U0001f552 Posted {age}")
    if exp > 0:
        lines.append(f"\U0001f4c5 {exp}+ years experience")
    if sal:
        lines.append(f"\U0001f4b0 {sal}")
    if bd:
        lines.append(f"\U0001f4ca {bd}")

    if req or nice:
        lines.append("")
        if req:
            lines.append(f"\u2705 <b>Required:</b> {req}")
        if nice:
            lines.append(f"\U0001f7e1 <b>Nice to have:</b> {nice}")

    lines.extend(["", f"\U0001f517 {job['url']}"])

    return "\n".join(lines)


def job_inline_keyboard(job):
    return {
        "inline_keyboard": [
            [
                {"text": "\u2705 Interested", "callback_data": f"interested:{job['id']}"},
                {"text": "\u274c Skip", "callback_data": f"skip:{job['id']}"},
                {"text": "\U0001f4c4 Details", "url": job["url"]},
            ]
        ]
    }


def notify_new_jobs(token, chat_id, jobs):
    if not jobs:
        return

    sorted_jobs = sorted(jobs, key=lambda j: j["score"], reverse=True)

    send_telegram(token, chat_id, f"<b>\U0001f4bc {len(sorted_jobs)} new matching job(s) found!</b>")
    time.sleep(1)

    for job in sorted_jobs:
        msg = format_job_message(job)
        keyboard = job_inline_keyboard(job)
        if send_telegram(token, chat_id, msg, reply_markup=keyboard):
            log.info(f"Sent: {job['title']} @ {job['company']}")
        else:
            log.warning(f"Failed to send: {job['title']}")
        time.sleep(1)


# ── Main ─────────────────────────────────────────────────────────────────────

SCRAPERS = [
    ("LinkedIn", scrape_linkedin),
    ("Foundit", scrape_foundit),
]


def parse_args():
    parser = argparse.ArgumentParser(description="Job scraper and utilities")
    parser.add_argument("--profile", metavar="NAME", help="Load profile from data/<NAME>/config.json")
    parser.add_argument("--collect-only", action="store_true", help="Scrape and store matches without sending notifications")
    parser.add_argument("--get-job", metavar="ID", help="Print job JSON to stdout")
    parser.add_argument("--send-doc", metavar="PATH", help="Send document via Telegram/ntfy")
    parser.add_argument("--send-msg", metavar="TEXT", help="Send message via Telegram/ntfy")
    parser.add_argument("--mark-interested", metavar="ID", help="Mark job as interested in DB")
    parser.add_argument("--job-stats", action="store_true", help="Print JSON backlog/status counters")
    parser.add_argument("--archive-stale-days", type=int, metavar="DAYS", help="Archive unnotified new jobs older than DAYS")
    parser.add_argument("--dry-run", action="store_true", help="Preview write actions such as --archive-stale-days")
    parser.add_argument("caption", nargs="?", default=None, help="Optional caption for --send-doc")
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()

    # Load profile config (None = default/backward compat)
    global CONFIG
    CONFIG = load_profile_config(args.profile)

    # ── CLI utility commands (no logging setup needed) ────────────────────────
    if args.get_job:
        conn = init_db()
        job = get_job_by_id(conn, args.get_job)
        conn.close()
        if job is None:
            print(json.dumps({"error": f"Job not found: {args.get_job}"}), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(job, indent=2))
        return

    if args.mark_interested:
        conn = init_db()
        mark_interested(conn, args.mark_interested)
        conn.close()
        print(json.dumps({"ok": True, "job_id": args.mark_interested}))
        return

    if args.job_stats:
        conn = init_db()
        summary = get_job_status_summary(conn)
        conn.close()
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    if args.archive_stale_days is not None:
        if args.archive_stale_days < 1:
            print("--archive-stale-days must be >= 1", file=sys.stderr)
            sys.exit(1)
        conn = init_db()
        archived = archive_stale_unreviewed_jobs(
            conn,
            older_than_days=args.archive_stale_days,
            dry_run=args.dry_run,
        )
        summary = get_job_status_summary(conn)
        conn.close()
        print(json.dumps({
            "ok": True,
            "dry_run": args.dry_run,
            "archive_stale_days": args.archive_stale_days,
            "matched_jobs": archived,
            "summary": summary,
        }, indent=2, sort_keys=True))
        return

    # Determine notification backend
    notif_config = CONFIG.get("notification", {})
    notif_type = notif_config.get("type", "telegram")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    ntfy_publish_topic = notif_config.get("publish_topic")

    if args.send_doc:
        if notif_type == "ntfy":
            if not ntfy_publish_topic:
                print("ntfy publish_topic not configured in profile", file=sys.stderr)
                sys.exit(1)
            ok = send_ntfy_file(ntfy_publish_topic, args.send_doc, filename=Path(args.send_doc).name)
        else:
            if not token or not chat_id:
                print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set", file=sys.stderr)
                sys.exit(1)
            ok = send_telegram_document(token, chat_id, args.send_doc, args.caption)
        sys.exit(0 if ok else 1)

    if args.send_msg:
        if notif_type == "ntfy":
            if not ntfy_publish_topic:
                print("ntfy publish_topic not configured in profile", file=sys.stderr)
                sys.exit(1)
            ok = send_ntfy(ntfy_publish_topic, "Job Hunter", args.send_msg)
        else:
            if not token or not chat_id:
                print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set", file=sys.stderr)
                sys.exit(1)
            ok = send_telegram(token, chat_id, args.send_msg)
        sys.exit(0 if ok else 1)

    # ── Normal scraping mode ──────────────────────────────────────────────────
    setup_logging()

    if notif_type == "ntfy":
        notifications_enabled = bool(ntfy_publish_topic)
        if not notifications_enabled:
            log.warning("ntfy publish_topic not configured — running without notifications")
    else:
        notifications_enabled = bool(token and chat_id)
        if not notifications_enabled:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — running without notifications")

    log.info("Starting job scraper")

    conn = init_db()
    session = create_session()
    new_jobs = []
    target = CONFIG["min_matching_jobs"]

    # Build all (scraper, region) buckets with their keyword×location generators
    buckets = {}
    for scraper_name, scraper_fn in SCRAPERS:
        for region_name, locations in CONFIG["regions"].items():
            bucket = f"{scraper_name}/{region_name}"
            # Create a page generator for each keyword×location combo
            generators = []
            for keyword in CONFIG["keywords"]:
                for location in locations:
                    generators.append(scraper_fn(session, keyword, location))
            buckets[bucket] = {
                "matches": 0,
                "generators": generators,  # page generators (each yields list of jobs)
                "pending_jobs": [],  # jobs fetched but not yet evaluated
            }

    def evaluate_job(job):
        """Evaluate a single job: fetch details, filter, score. Returns job if it passes, None otherwise."""
        if is_job_seen(conn, job["id"]):
            return None
        if is_excluded(job):
            log.debug(f"Excluded: {job['title']}")
            return None
        if not is_allowed_location(job):
            log.info(f"Skipped (outside allowed location): {job['title']} @ {job['company']} — {job.get('location', '')}")
            return None

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
                    return None
            except (ValueError, TypeError):
                pass

        log.info(f"Fetching details: {job['title']}")
        desc = fetch_job_description(session, job)
        job["description"] = desc

        if not CONFIG.get("skip_local_presence", False) and requires_local_presence(desc):
            log.info(f"Skipped (requires local presence): {job['title']} @ {job['company']}")
            return None

        req, nice = extract_tech_keywords(desc)
        job["tech_required"] = ", ".join(req)
        job["tech_nice_to_have"] = ", ".join(nice)
        job["min_experience"] = extract_min_experience(desc)

        max_exp = CONFIG["max_experience"]
        if job["min_experience"] > max_exp:
            log.info(f"Skipped ({job['min_experience']}+ yrs > {max_exp} max): {job['title']} @ {job['company']}")
            return None

        job["salary"] = extract_salary(desc)
        job["work_model"] = detect_work_model(desc)
        score, breakdown = score_job(job)
        job["score"] = score
        job["score_breakdown"] = ", ".join(breakdown)
        save_job(conn, job)
        return job

    # Breadth-first: fetch one page per generator, evaluate immediately, rotate
    while any(b["matches"] < target and b["generators"] for b in buckets.values()):
        for bucket, state in buckets.items():
            if state["matches"] >= target:
                continue
            if not state["generators"]:
                continue

            # Fetch one page from each generator, evaluate after each page
            next_generators = []
            for gen in state["generators"]:
                if state["matches"] >= target:
                    next_generators.append(gen)  # keep for later (won't be used)
                    continue
                try:
                    page_jobs = next(gen)
                except StopIteration:
                    continue
                except Exception as e:
                    log.exception(f"{bucket} scraper failed: {e}")
                    continue

                next_generators.append(gen)

                # Evaluate jobs from this page immediately
                for job in page_jobs:
                    if state["matches"] >= target:
                        break

                    result = evaluate_job(job)
                    if result and result["score"] >= CONFIG["score_threshold"]:
                        new_jobs.append(result)
                        state["matches"] += 1
                        count = state["matches"]
                        age = job_age(result.get("date_posted", ""))
                        age_str = f" | {age}" if age else ""
                        exp_str = f" | {result['min_experience']}+ yrs" if result["min_experience"] > 0 else ""
                        sal_str = f" | {result['salary']}" if result["salary"] else ""
                        wm_str = f" | {result['work_model']}" if result["work_model"] != "on-site" else ""
                        log.info(f"New match [{bucket} {count}/{target}]: {result['title']} @ {result['company']} (score={result['score']}{age_str}{exp_str}{sal_str}{wm_str})")
                        log.info(f"        Score: {result['score_breakdown']}")

                        if count >= target:
                            log.info(f"Reached {target} matches for {bucket}, moving on")

            state["generators"] = next_generators

    if args.collect_only:
        log.info(f"Collect-only mode: stored {len(new_jobs)} threshold-matching job(s); notifications skipped")
    elif new_jobs:
        log.info(f"Found {len(new_jobs)} new matching job(s)")
        if notifications_enabled:
            if notif_type == "ntfy":
                notify_new_jobs_ntfy(ntfy_publish_topic, new_jobs)
            else:
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
