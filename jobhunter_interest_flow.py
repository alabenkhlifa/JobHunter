"""Interested-job research and application-package helpers for JobHunter.

This module is intentionally deterministic and testable. Live web research can
feed a JobResearch object, but the formatting/state transitions here avoid
network calls so Telegram callbacks remain reliable.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from typing import Any

import scraper
from resume_refiner import (
    apply_resume_variant,
    project_public_resume,
    select_resume_variant,
    usable_evidence,
    validate_profile,
)

DEFAULT_TARGET_SALARY_AED_MONTHLY = 30000
COMPANY_PAY_PLATFORMS = ("Glassdoor", "Indeed", "PayScale", "GulfTalent", "Levels.fyi")
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "jobs.db"
DEFAULT_PROFILE_PATH = PROJECT_DIR / "data" / "master-profile.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "output"

_RELEVANCE_GROUPS = (
    ("backend", "server-side", "microservice", "spring boot", "rest api", "api"),
    ("distributed", "event-driven", "message queue", "rabbitmq", "mqtt"),
    ("cloud", "aws", "azure", "kubernetes", "docker", "terraform"),
    (
        "architecture",
        "system design",
        "solution design",
        "cloud-native",
        "saas",
        "multi-tenant",
        "service mesh",
    ),
    ("security", "zero-trust", "privacy by design", "data residency", "compliance"),
    ("gitops", "argocd", "fluxcd", "progressive deployment", "blue-green"),
    ("observability", "monitoring", "logging", "reliability", "sre"),
    ("database", "mysql", "postgresql", "mongodb", "nosql", "redis", "cache"),
    ("performance", "scalable", "scalability", "load testing"),
    ("java", "kotlin", "jvm", "spring"),
    ("golang", "pprof"),
    ("ai", "machine learning", "ml", "rag", "llm", "mlops", "llmops"),
    ("lead", "leadership", "mentoring", "stakeholder", "cross-functional"),
)

_KEYWORD_STOPWORDS = {
    "about", "after", "also", "being", "build", "company", "could", "from", "have",
    "development", "engineering", "experience", "including", "into", "management", "other",
    "product", "production", "responsible", "role", "service", "services", "software",
    "strong", "system", "systems", "team", "technical", "technology", "their", "these", "through",
    "using", "with", "work", "years", "your",
}

_SKILL_SIGNALS = (
    ("java", ("java", "jvm"), ("java",), 10),
    ("java_backend", ("java", "jvm"), ("spring boot",), 6),
    ("backend", ("backend", "server-side"), ("spring boot", "microservices", "rest api"), 5),
    (
        "distributed",
        ("distributed", "message queue", "distributed storage"),
        ("event-driven", "rabbitmq", "mqtt", "microservices"),
        8,
    ),
    ("database", ("mysql", "nosql", "database", "cache"), ("postgresql", "mongodb", "redis"), 7),
    ("cache", ("cache",), ("redis",), 5),
    ("nosql", ("nosql",), ("mongodb",), 5),
    ("cloud", ("cloud", "kubernetes", "container"), ("aws", "azure", "kubernetes", "docker"), 5),
    ("ai", ("ai / ml", "ai/ml", "machine learning"), ("ai", "rag", "llm"), 4),
)


@dataclass
class JobResearch:
    company_summary: str
    legitimacy: str
    recruiter: str = "Not found"
    salary_range: str = "Salary not published; use configured target as anchor."
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: str = "Low"
    verified_signals: list[str] = field(default_factory=list)
    missing_signals: list[str] = field(default_factory=list)
    recommendation: str = "Use Details to verify the official application path before investing time."
    salary_sources: list[dict[str, str]] = field(default_factory=list)
    company_salary_sources: list[dict[str, str]] = field(default_factory=list)
    company_salary_checks: list[str] = field(default_factory=list)
    employer_name: str = ""
    posting_company: str = ""


@dataclass
class ApplicationPackage:
    job_id: str
    package_dir: Path
    resume_json: Path
    cover_json: Path
    resume_pdf: Path
    cover_pdf: Path
    manifest_json: Path | None = None


class TailoringReadinessError(RuntimeError):
    """Raised when a safe, role-appropriate application package cannot be generated."""


def target_salary_aed_monthly() -> int:
    raw = os.getenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "").strip()
    if not raw:
        return DEFAULT_TARGET_SALARY_AED_MONTHLY
    digits = re.sub(r"[^0-9]", "", raw)
    return int(digits) if digits else DEFAULT_TARGET_SALARY_AED_MONTHLY


def target_salary_label() -> str:
    value = target_salary_aed_monthly()
    if value % 1000 == 0:
        return f"AED {value // 1000}k/month"
    return f"AED {value:,}/month"


def web_research_enabled() -> bool:
    return os.getenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true").strip().lower() not in {"0", "false", "no", "off"}


def parse_duckduckgo_results(html_text: str, *, limit: int = 5) -> list[dict[str, str]]:
    """Extract compact result title/url/snippet triples from DuckDuckGo HTML.

    Kept small and dependency-light for callback use; tests cover this parser so
    network failures can safely fall back to stored metadata.
    """
    results: list[dict[str, str]] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text):
        href = html.unescape(match.group("href"))
        parsed = urlparse(href)
        if parsed.path.startswith("/l/"):
            href = unquote(parse_qs(parsed.query).get("uddg", [href])[0])
        title = re.sub(r"<[^>]+>", "", html.unescape(match.group("title")))
        snippet = re.sub(r"<[^>]+>", "", html.unescape(match.group("snippet")))
        if href and title:
            results.append({"title": " ".join(title.split()), "url": href, "snippet": " ".join(snippet.split())})
        if len(results) >= limit:
            break
    return results


def _env_value_from_file(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def firecrawl_api_url() -> str:
    return (
        os.getenv("FIRECRAWL_API_URL", "").strip()
        or _env_value_from_file(Path.home() / ".hermes" / ".env", "FIRECRAWL_API_URL")
    ).rstrip("/")


def firecrawl_search_results(query: str, *, timeout: float, poster=requests.post) -> list[dict[str, str]]:
    base_url = firecrawl_api_url()
    if not base_url:
        return []
    try:
        response = poster(
            f"{base_url}/v1/search",
            json={"query": query, "limit": 5},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except Exception:
        return []
    if response.status_code >= 400:
        return []
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else []
    results: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get("title") or "").split())
        url = str(row.get("url") or "").strip()
        snippet = " ".join(str(row.get("description") or row.get("snippet") or row.get("content") or "").split())
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def web_search_results(
    query: str,
    *,
    timeout: float | None = None,
    fetcher=requests.get,
    poster=requests.post,
) -> list[dict[str, str]]:
    timeout = timeout if timeout is not None else float(os.getenv("JOBHUNTER_WEB_RESEARCH_TIMEOUT", "8"))
    firecrawl_results = firecrawl_search_results(query, timeout=timeout, poster=poster)
    if firecrawl_results:
        return firecrawl_results
    try:
        response = fetcher(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 JobHunter research bot"},
            timeout=timeout,
        )
    except Exception:
        return []
    if response.status_code != 200:
        return []
    return parse_duckduckgo_results(response.text)


def salary_search_queries(title: str, location: str) -> list[str]:
    city = str(location or "Dubai").split(",", 1)[0].strip() or "Dubai"
    normalized_title = " ".join(str(title or "software architect").replace("/", " ").split())
    return [
        f"site:gulftalent.com UAE {normalized_title} salary",
        f"site:payscale.com Dubai {normalized_title} salary",
        f"site:glassdoor.com Dubai {normalized_title} salary",
        f"site:indeed.com Dubai {normalized_title} salary AED",
        f"UAE {normalized_title} salary AED monthly GulfTalent Glassdoor PayScale Indeed",
        f"{city} {normalized_title} salary AED monthly",
    ]


ROLE_FAMILY_PATTERNS = (
    ("Solutions Architect", r"\bsolutions?\s+architect\b"),
    ("Technical Program Manager", r"\btechnical\s+program\s+manager\b"),
    ("Machine Learning Engineer", r"\bmachine\s+learning\s+engineer\b"),
    ("Engineering Manager", r"\bengineering\s+manager\b"),
    ("Product Manager", r"\bproduct\s+manager\b"),
    ("Program Manager", r"\bprogram\s+manager\b"),
    ("Project Manager", r"\bproject\s+manager\b"),
    ("Software Engineer", r"\bsoftware\s+engineer\b"),
    ("Backend Engineer", r"\bback(?:end|-end)\s+engineer\b"),
    ("Frontend Engineer", r"\bfront(?:end|-end)\s+engineer\b"),
    ("Data Engineer", r"\bdata\s+engineer\b"),
    ("DevOps Engineer", r"\bdevops\s+engineer\b"),
    ("Security Engineer", r"\bsecurity\s+engineer\b"),
    ("Cloud Architect", r"\bcloud\s+architect\b"),
    ("Security Architect", r"\bsecurity\s+architect\b"),
    ("Enterprise Architect", r"\benterprise\s+architect\b"),
    ("Software Architect", r"\bsoftware\s+architect\b"),
    ("Data Architect", r"\bdata\s+architect\b"),
    ("Delivery Consultant", r"\bdelivery\s+consultant\b"),
    ("Cloud Consultant", r"\bcloud\s+consultant\b"),
    ("Business Analyst", r"\bbusiness\s+analyst\b"),
    ("Data Analyst", r"\bdata\s+analyst\b"),
    ("Security Analyst", r"\bsecurity\s+analyst\b"),
)


def salary_role_title(title: str) -> str:
    """Reduce a verbose vacancy title to the role used by salary sites."""
    value = " ".join(str(title or "").replace("/", " ").split())
    for label, pattern in ROLE_FAMILY_PATTERNS:
        if re.search(pattern, value, flags=re.IGNORECASE):
            return label
    fallback = re.split(r"\s*(?:,|\||\s[-–—]\s)\s*", value, maxsplit=1)[0]
    return " ".join(fallback.split()[:6])


def company_salary_search_queries(company: str, title: str, location: str) -> list[str]:
    city = str(location or "Dubai").split(",", 1)[0].strip() or "Dubai"
    role = salary_role_title(title)
    search_company = company_search_name(company)
    if not search_company:
        return []
    return [
        f'"{search_company}" careers compensation salary benefits',
        f'site:glassdoor.com/Salary "{search_company}" "{role}" {city}',
        f'site:indeed.com/cmp "{search_company}" "{role}" {city} salaries',
        f'site:payscale.com "{search_company}" "{role}" {city} salary',
        f'site:gulftalent.com "{search_company}" "{role}" {city} salary',
        f'site:levels.fyi/companies "{search_company}" "{role}" {city}',
    ]


def company_salary_check_labels(company: str) -> list[str]:
    company = " ".join(str(company or "company").split())
    return [
        f"{company} careers page",
        "Glassdoor company salary",
        "Indeed company salary",
        "PayScale company salary",
        "GulfTalent company salary",
        "Levels.fyi company salary",
    ]


def _salary_slug(value: str) -> str:
    normalized = str(value or "").replace("&", "")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def _salary_city(location: str) -> str:
    country_labels = {
        "france", "germany", "ksa", "netherlands", "saudi arabia", "spain", "uae",
        "united arab emirates", "united kingdom", "united states", "usa",
    }
    parts = [part.strip() for part in str(location or "").split(",") if part.strip()]
    return next((part for part in parts if part.lower() not in country_labels), parts[0] if parts else "")


def levels_salary_url(company: str, title: str, location: str) -> str:
    company_slug = _salary_slug(company_search_name(company))
    role_slug = _salary_slug(salary_role_title(title)).replace("solutions-architect", "solution-architect")
    city_slug = _salary_slug(_salary_city(location))
    if not company_slug or not role_slug or not city_slug:
        return ""
    location_slug = city_slug if city_slug.startswith("greater-") else f"greater-{city_slug}-area"
    return f"https://www.levels.fyi/companies/{company_slug}/salaries/{role_slug}/locations/{location_slug}"


def fetch_levels_salary_source(
    company: str,
    title: str,
    location: str,
    *,
    timeout: float | None = None,
    poster=requests.post,
) -> dict[str, str] | None:
    """Fetch a predictable Levels.fyi page through Firecrawl and validate its contents."""
    base_url = firecrawl_api_url()
    salary_url = levels_salary_url(company, title, location)
    if not base_url or not salary_url:
        return None
    locale_url = salary_url.replace("https://www.levels.fyi/", "https://www.levels.fyi/en-gb/", 1)
    for candidate_url in (salary_url, locale_url):
        try:
            response = poster(
                f"{base_url}/v1/scrape",
                json={"url": candidate_url, "formats": ["markdown"]},
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code >= 400:
                continue
            payload = response.json()
        except Exception:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        markdown = str((data or {}).get("markdown") or (data or {}).get("content") or "")
        if not markdown:
            continue

        lines = [" ".join(line.split()) for line in markdown.splitlines() if line.strip()]
        salary_line = next(
            (
                line for line in lines
                if "aed" in line.lower()
                and any(term in line.lower() for term in ("compensation", "salary", "pay", "ranges from"))
            ),
            "",
        )
        if salary_line:
            salary_line = re.split(r"(?<=[A-Za-z0-9])\.(?=\s+[A-Z])", salary_line, maxsplit=1)[0].rstrip(".") + "."
        evidence = {"title": " ".join(lines[:12]), "url": "", "snippet": salary_line}
        location_evidence = {"title": "", "url": candidate_url, "snippet": f"{salary_line} {' '.join(lines[:20])}"}
        if (
            not salary_line
            or not _result_matches_company(company, evidence)
            or not _result_matches_role(title, evidence)
            or not _salary_source_matches_job_location(location_evidence, location)
        ):
            continue
        return {
            "source": "Levels.fyi",
            "title": f"{company_search_name(company)} {salary_role_title(title)} salary in {_salary_city(location)}",
            "url": candidate_url,
            "snippet": salary_line[:220],
        }
    return None


def _salary_source_name(url: str, title: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if "gulftalent" in host:
        return "GulfTalent"
    if "glassdoor" in host:
        return "Glassdoor"
    if "payscale" in host:
        return "PayScale"
    if "indeed" in host:
        return "Indeed"
    if "levels.fyi" in host:
        return "Levels.fyi"
    if "salaryexpert" in host:
        return "SalaryExpert"
    return host or title.split(" - ")[0]


COMPANY_LEGAL_SUFFIXES = {"fz", "fze", "llc", "ltd", "limited", "inc", "corp", "company", "plc"}
COMPANY_GENERIC_WORDS = {
    "cloud", "digital", "global", "group", "holding", "international", "software",
    "solutions", "systems", "technologies", "technology", "web", "services",
}


def company_identity_aliases(company: str) -> list[str]:
    """Return conservative public-name variants without company-specific rules."""
    raw = " ".join(str(company or "").split())
    if not raw:
        return []
    parenthetical = re.findall(r"\(([^()]{2,30})\)", raw)
    base = " ".join(re.sub(r"\([^()]*\)", " ", raw).split()).strip(" ,-–—")
    legal_pattern = "|".join(re.escape(value) for value in sorted(COMPANY_LEGAL_SUFFIXES, key=len, reverse=True))
    base = re.sub(rf"(?:[\s,.-]+(?:{legal_pattern}))+$", "", base, flags=re.IGNORECASE).strip(" ,-–—")
    words = re.findall(r"[A-Za-z0-9]+", base)

    aliases = [base]
    distinctive = [word for word in words if word.lower() not in COMPANY_GENERIC_WORDS]
    if len(distinctive) == 1 and len(distinctive[0]) >= 4:
        aliases.append(distinctive[0])
    elif words and len(words[0]) >= 4 and words[0].lower() not in COMPANY_GENERIC_WORDS:
        aliases.append(words[0])
    aliases.extend(value.strip() for value in parenthetical)
    if len(words) >= 2:
        aliases.append("".join(word[0] for word in words).upper())

    deduped: list[str] = []
    seen_tokens: set[str] = set()
    for alias in aliases:
        token = re.sub(r"[^a-z0-9]", "", alias.lower())
        short_public_name = len(token) >= 2 and alias.upper() == alias
        if (len(token) >= 3 or short_public_name) and token not in seen_tokens:
            seen_tokens.add(token)
            deduped.append(alias)
    return deduped


def company_search_name(company: str) -> str:
    aliases = company_identity_aliases(company)
    if not aliases:
        return ""
    single_brand = next(
        (alias for alias in aliases[1:] if " " not in alias and len(alias) >= 4 and not alias.isupper()),
        "",
    )
    explicit_parenthetical = {
        re.sub(r"[^a-z0-9]", "", value.lower())
        for value in re.findall(r"\(([^()]{2,30})\)", str(company or ""))
    }
    public_acronym = next(
        (
            alias for alias in aliases[1:]
            if re.sub(r"[^a-z0-9]", "", alias.lower()) in explicit_parenthetical
            and alias.isupper()
        ),
        "",
    )
    return single_brand or public_acronym or aliases[0]


def _result_matches_company(company: str, result: dict[str, str]) -> bool:
    """Require the employer identity in the title or URL, never only the snippet."""
    aliases = company_identity_aliases(company)
    title_token = re.sub(r"[^a-z0-9]", "", str(result.get("title") or "").lower())
    parsed = urlparse(str(result.get("url") or ""))
    url_token = re.sub(r"[^a-z0-9]", "", f"{parsed.netloc}{parsed.path}".lower())
    boundary_text = f"{result.get('title') or ''} {parsed.netloc} {parsed.path}".lower()
    for alias in aliases:
        alias_token = re.sub(r"[^a-z0-9]", "", alias.lower())
        if len(alias_token) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", boundary_text):
                return True
        elif alias_token in title_token or alias_token in url_token:
            return True
    return False


def _result_matches_role(title: str, result: dict[str, str]) -> bool:
    role = salary_role_title(title)
    if not role:
        return True
    role_token = re.sub(r"[^a-z0-9]", "", role.lower()).replace("solutions", "solution")
    result_token = re.sub(
        r"[^a-z0-9]",
        "",
        f"{result.get('title') or ''} {result.get('url') or ''} {result.get('snippet') or ''}".lower(),
    ).replace("solutions", "solution")
    return role_token in result_token


def _is_official_company_result(company: str, url: str) -> bool:
    host = urlparse(str(url or "")).netloc.lower().removeprefix("www.")
    host_token = re.sub(r"[^a-z0-9]", "", host)
    host_labels = {re.sub(r"[^a-z0-9]", "", label) for label in host.split(".")}
    blocked_hosts = ("glassdoor", "indeed", "payscale", "gulftalent", "levels", "linkedin")
    alias_tokens = [
        re.sub(r"[^a-z0-9]", "", alias.lower())
        for alias in company_identity_aliases(company)
    ]
    return bool(
        not any(blocked in host_token for blocked in blocked_hosts)
        and any(
            (len(token) >= 3 and token in host_token) or (len(token) == 2 and token in host_labels)
            for token in alias_tokens
        )
    )


def resolve_research_employer(job: dict[str, Any]) -> tuple[str, str]:
    """Return (real employer, posting company) when a strong aggregator pattern exists."""
    posting_company = " ".join(str(job.get("company") or "").split())
    employer = scraper.extract_actual_employer(
        posting_company,
        str(job.get("description") or ""),
        str(job.get("credibility_notes") or ""),
    )
    return (employer, posting_company) if employer != posting_company else (posting_company, "")


def validated_job_salary(job: dict[str, Any]) -> str:
    """Revalidate legacy stored salary text against its labelled location."""
    stored = str(job.get("salary") or "").strip()
    if not stored:
        return ""
    description = str(job.get("description") or "")
    normalized_stored = re.sub(r"[\s,]+", "", stored).lower()
    normalized_description = re.sub(r"[\s,]+", "", description).lower()
    if description and normalized_stored and normalized_stored in normalized_description:
        return scraper.extract_salary(description, str(job.get("location") or ""))
    return stored


def company_profile_search_queries(company: str, title: str, location: str) -> list[str]:
    city = str(location or "Dubai").split(",", 1)[0].strip() or "Dubai"
    return [
        f'"{company}" {city} company official about',
        f'site:linkedin.com/company "{company}" {city}',
        f'"{company}" {city} "{title}"',
    ]


def _compact_company_summary(company: str, location: str, results: list[dict[str, str]]) -> str:
    city = str(location or "").split(",", 1)[0].strip()
    text = " ".join(f"{item.get('title', '')} {item.get('snippet', '')}" for item in results)
    lowered = text.lower()
    financial_product_terms = sum(
        term in lowered for term in ("spending", "saving", "investing", "exchanging")
    )
    if "database platform" in lowered or "database technology" in lowered:
        company_type = "database technology company"
    elif any(term in lowered for term in ("cloud services", "cloud provider", "aws cloud", "migrate to the cloud")):
        company_type = "cloud technology company"
    elif "fintech" in lowered or "financial technology" in lowered or financial_product_terms >= 3:
        company_type = "financial technology company"
    elif any(term in lowered for term in ("technology consultancy", "technology consulting", "tech consultancy")):
        company_type = "technology consultancy"
    elif any(term in lowered for term in ("software company", "software development")):
        company_type = "software company"
    else:
        company_type = "technology company"

    focus: list[str] = []
    if "legacy" in lowered and any(term in lowered for term in ("modernisation", "modernization", "modernise", "modernize")):
        focus.append("legacy-system modernization")
    if any(term in lowered for term in ("systems integration", "system integration", "apis", "api architecture")):
        focus.append("systems integration")
    if any(term in lowered for term in ("software architecture", "solution architecture", "architecture design")):
        focus.append("software architecture")
    if "cloud" in lowered and company_type != "cloud technology company":
        focus.append("cloud platforms")
    if "artificial intelligence" in lowered or "machine learning" in lowered or re.search(r"\bai\b", lowered):
        focus.append("AI")
    if financial_product_terms >= 3:
        focus.append("digital financial services")

    company_pattern = re.escape(str(company or "").lower())
    city_pattern = re.escape(city.lower()) if city else ""
    explicit_city = bool(
        city_pattern
        and re.search(
            rf"\b{company_pattern}\b.{{0,120}}(?:is\s+(?:an?\s+)?{city_pattern}-based|headquartered\s+in\s+{city_pattern}|registered\s+in\s+{city_pattern})",
            lowered,
        )
    )
    global_scope = any(term in lowered for term in ("global", "worldwide", "around the world"))
    scope = f"{city}-based " if explicit_city else ("global " if global_scope else "")
    base = f"{company} is a {scope}{company_type}"
    if focus:
        base += f" focused on {', '.join(dict.fromkeys(focus[:3]))}"
    size_match = re.search(r"\b(\d{1,4})\s*(?:-|–|to)\s*(\d{1,4})\s+employees\b", lowered)
    facts: list[str] = []
    if size_match:
        facts.append(f"{size_match.group(1)}–{size_match.group(2)} employees")
    else:
        employee_match = re.search(r"\b(\d{1,3}(?:,\d{3})*\+?)\s+(?:employees|people working)\b", lowered)
        if employee_match:
            facts.append(f"{employee_match.group(1)} employees")
    customer_match = re.search(r"\b(\d+\+?\s+million)\s+customers\b", lowered)
    if customer_match:
        facts.append(f"{customer_match.group(1)} customers")
    else:
        customer_match = re.search(r"\b(\d{1,3}(?:,\d{3})*\+?)\s+customers\b", lowered)
        if customer_match:
            facts.append(f"{customer_match.group(1)} customers")
    if facts:
        base += f" ({'; '.join(facts[:2])})"
    return base + "."


def _company_summary_from_job_description(company: str, description: str) -> str:
    """Use the employer's own About section as a labelled fallback."""
    value = " ".join(str(description or "").split())
    company_pattern = re.escape(str(company or "").strip())
    about_match = re.search(rf"\bAbout\s+{company_pattern}\b", value, flags=re.IGNORECASE) if company_pattern else None
    if not about_match:
        alias_matches = [
            match
            for alias in company_identity_aliases(company)
            if (match := re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", value, flags=re.IGNORECASE))
        ]
        if not alias_matches:
            return ""
        first_match = min(alias_matches, key=lambda match: match.start())
        context = value[max(0, first_match.start() - 120):first_match.start() + 900]
        context = re.split(
            r"\b(?:Key\s+Job\s+Responsibilities|Key\s+Responsibilities|Responsibilities|Requirements|Qualifications)\b",
            context,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        if not any(
            term in context.lower()
            for term in ("customers", "platform", "products", "services", "software", "technology", "cloud", "database")
        ):
            return ""
        return _compact_company_summary(company, "", [{"snippet": context}])
    about_tail = value[about_match.start():]
    about_section = re.split(
        r"\b(?:About\s+(?:The\s+)?Role|What\s+You(?:'|’)ll\s+Be\s+Doing|Responsibilities|Requirements)\b",
        about_tail,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0][:2200]
    if len(about_section) < 80:
        return ""
    return _compact_company_summary(company, "", [{"snippet": about_section}])


def _company_summary_has_useful_detail(company: str, summary: str) -> bool:
    normalized = " ".join(str(summary or "").split())
    return bool(normalized and normalized != f"{company} is a technology company.")


def _plain_text_from_html(html_text: str, *, limit: int = 6000) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())[:limit]


def _excerpt_around_terms(text: str, terms: tuple[str, ...], *, max_len: int = 220) -> str:
    value = " ".join(str(text or "").split())
    lowered = value.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    if not positions:
        return value[:max_len]
    start = min(positions)
    end = min(len(value), start + max_len)
    return value[start:end].strip()


def fetch_verified_company_pages(
    company: str,
    sources: list[dict[str, str]],
    *,
    fetcher=requests.get,
    timeout: float = 5,
) -> list[dict[str, str]]:
    """Fetch verified company-domain pages.

    Prefer an official domain discovered by search. If search is noisy/empty,
    probe a small set of likely employer-owned domains and accept only pages
    whose final host still contains the normalized company token.
    """
    company_token = re.sub(r"[^a-z0-9]", "", company_search_name(company).lower())
    official_url = next(
        (str(item.get("url") or "") for item in sources if _is_official_company_result(company, str(item.get("url") or ""))),
        "",
    )
    candidates: list[str] = []
    parsed = urlparse(official_url)
    if parsed.scheme and parsed.netloc:
        candidates.append(f"{parsed.scheme}://{parsed.netloc}")
    elif company_token:
        candidates.extend(f"https://{company_token}.{tld}" for tld in ("ae", "com", "io", "ai"))

    def fetch(url: str) -> dict[str, str] | None:
        try:
            response = fetcher(
                url,
                headers={"User-Agent": "Mozilla/5.0 JobHunter research bot"},
                timeout=timeout,
                allow_redirects=True,
            )
        except Exception:
            return None
        final_url = str(getattr(response, "url", url))
        final_host = urlparse(final_url).netloc.lower().removeprefix("www.")
        final_host_token = re.sub(r"[^a-z0-9]", "", final_host)
        if response.status_code >= 400 or "html" not in response.headers.get("content-type", "").lower():
            return None
        if not company_token or company_token not in final_host_token:
            return None
        snippet = _plain_text_from_html(response.text)
        snippet_token = re.sub(r"[^a-z0-9]", "", snippet.lower()[:1200])
        if company_token not in snippet_token and not any(term in snippet.lower() for term in ("careers", "about", "compensation", "open positions")):
            return None
        return {"title": final_url, "url": final_url, "snippet": snippet}

    def fetch_parallel(urls: list[str]) -> list[dict[str, str]]:
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=min(len(urls), 4)) as executor:
            return [result for result in executor.map(fetch, urls) if result]

    if official_url:
        urls = [candidates[0].rstrip("/") + path for path in ("/career/", "/careers/", "/about/", "/")]
        results = fetch_parallel(urls)
    else:
        # Probe one root per likely domain first. Expanding every guessed domain
        # to four paths can multiply a single timeout into a 30+ second callback.
        root_results = fetch_parallel([base.rstrip("/") + "/" for base in candidates])
        verified_bases = list(dict.fromkeys(
            f"{urlparse(item['url']).scheme}://{urlparse(item['url']).netloc}"
            for item in root_results
        ))
        detail_urls = [
            base.rstrip("/") + path
            for base in verified_bases
            for path in ("/career/", "/careers/", "/about/")
        ]
        results = [*root_results, *fetch_parallel(detail_urls)]
    deduped: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in results:
        url = result["url"]
        if url not in seen_urls:
            seen_urls.add(url)
            deduped.append(result)
    return deduped[:3]


def collect_company_salary_sources(
    company: str,
    title: str,
    location: str,
    *,
    max_sources: int = 4,
    timeout: float | None = None,
) -> list[dict[str, str]]:
    seen_urls: set[str] = set()
    sources: list[dict[str, str]] = []
    queries = company_salary_search_queries(company, title, location)
    if not queries:
        return sources

    def run_search(query: str) -> list[dict[str, str]]:
        return web_search_results(query, timeout=timeout)[:5]

    with ThreadPoolExecutor(max_workers=min(len(queries), 6)) as executor:
        result_groups = list(executor.map(run_search, queries))

    for results in result_groups:
        for result in results:
            url = result.get("url", "")
            text = f"{result.get('title', '')} {url} {result.get('snippet', '')}".lower()
            if not url or url in seen_urls or not _result_matches_company(company, result):
                continue
            official_result = _is_official_company_result(company, url)
            if not official_result and not _result_matches_role(title, result):
                continue
            if not any(term in text for term in ("salary", "salaries", "aed", "pay", "compensation", "bonus", "equity", "profit sharing")):
                continue
            seen_urls.add(url)
            sources.append({
                "source": "Company careers page" if official_result else _salary_source_name(url, result.get("title", "")),
                "title": result.get("title", ""),
                "url": url,
                "snippet": result.get("snippet", ""),
            })
    if (
        not any(source.get("source") == "Levels.fyi" for source in sources)
    ):
        levels_source = fetch_levels_salary_source(company, title, location, timeout=timeout)
        if levels_source:
            sources.insert(0, levels_source)
    return sources[:max_sources]


def collect_salary_sources(title: str, location: str, *, max_sources: int = 3, timeout: float | None = None) -> list[dict[str, str]]:
    trusted_domains = ("gulftalent.com", "payscale.com", "glassdoor.com", "indeed.com", "salaryexpert.com")
    seen_domains: set[str] = set()
    trusted: list[dict[str, str]] = []
    fallback: list[dict[str, str]] = []
    for query in salary_search_queries(title, location):
        for result in web_search_results(query, timeout=timeout)[:5]:
            url = result.get("url", "")
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
            if not domain or domain in seen_domains:
                continue
            if not any(term in text for term in ("salary", "salaries", "aed", "pay", "compensation")):
                continue
            seen_domains.add(domain)
            item = {
                "source": _salary_source_name(url, result.get("title", "")),
                "title": result.get("title", ""),
                "url": url,
                "snippet": result.get("snippet", ""),
            }
            if any(domain == trusted_domain or domain.endswith("." + trusted_domain) for trusted_domain in trusted_domains):
                trusted.append(item)
            else:
                fallback.append(item)
            combined = [*trusted, *fallback]
            if len(trusted) >= max_sources:
                return trusted[:max_sources]
            if len(combined) >= max_sources and len(trusted) >= 2:
                return combined[:max_sources]
    return [*trusted, *fallback][:max_sources]


def research_job(job: dict[str, Any]) -> JobResearch:
    """Build a brief company/recruiter/salary research card with web fallback.

    Web lookup is best-effort and warning-only. If search fails, the returned
    brief still contains stored metadata and salary guidance.
    """
    employer, posting_company = resolve_research_employer(job)
    research_job_data = dict(job)
    research_job_data["company"] = employer
    research_job_data["salary"] = validated_job_salary(job)
    research = build_default_research(research_job_data)
    research.employer_name = employer
    research.posting_company = posting_company
    if not web_research_enabled():
        return research
    company = employer
    title = job.get("title") or ""
    location = job.get("location") or "Dubai"
    job_post_company_summary = _company_summary_from_job_description(
        str(company), str(job.get("description") or "")
    )
    has_job_post_company_summary = _company_summary_has_useful_detail(
        str(company), job_post_company_summary
    )
    try:
        timeout = float(os.getenv("JOBHUNTER_WEB_RESEARCH_TIMEOUT", "12"))
        search_timeout = min(timeout, 6)
        company_salary_sources = collect_company_salary_sources(
            str(company), str(title), str(location), timeout=search_timeout
        )
        official_search_results = [
            item for item in company_salary_sources
            if _is_official_company_result(str(company), str(item.get("url", "")))
        ]
        verified_pages = (
            fetch_verified_company_pages(str(company), official_search_results, timeout=min(timeout, 8))
            if official_search_results or not has_job_post_company_summary
            else []
        )
        if not company_salary_sources:
            for page in verified_pages:
                page_text = f"{page.get('title', '')} {page.get('snippet', '')}".lower()
                if any(term in page_text for term in ("compensation", "bonus", "equity", "profit sharing", "salary", "pay")):
                    company_salary_sources.append({
                        "source": "Company careers page",
                        "title": page.get("title", ""),
                        "url": page.get("url", ""),
                        "snippet": _excerpt_around_terms(
                            page.get("snippet", ""),
                            ("compensation", "bonus", "equity", "profit sharing", "salary", "pay"),
                        ),
                    })
                    break
        profile_results: list[dict[str, str]] = []
        if not verified_pages and not has_job_post_company_summary:
            profile_queries = company_profile_search_queries(str(company), str(title), str(location))
            with ThreadPoolExecutor(max_workers=len(profile_queries)) as executor:
                profile_result_groups = list(
                    executor.map(lambda query: web_search_results(query, timeout=search_timeout)[:5], profile_queries)
                )
            profile_results = [
                result for group in profile_result_groups for result in group
                if _result_matches_company(str(company), result)
            ]
        combined_company_results: list[dict[str, str]] = []
        seen_company_urls: set[str] = set()
        for result in [*verified_pages, *official_search_results, *profile_results]:
            url = result.get("url", "")
            if url and url not in seen_company_urls:
                seen_company_urls.add(url)
                combined_company_results.append(result)
        company_results = combined_company_results[:3]
    except Exception as exc:  # noqa: BLE001 - callback must stay reliable
        research.warnings.append(f"Web research unavailable: {exc.__class__.__name__}.")
        return research
    if company_results:
        research.company_summary = _compact_company_summary(str(company), str(location), company_results)
        research.confidence = "Medium"
        research.verified_signals.append(f"Web result found: {company_results[0]['title']}.")
        official_results = [
            r for r in company_results
            if _is_official_company_result(str(company), str(r.get("url", "")))
        ]
        if official_results:
            official = official_results[0]
            research.verified_signals.append(f"Official company/careers page found: {official['url']}.")
            research.missing_signals = [
                item for item in research.missing_signals
                if "Official company website/careers page" not in item
            ]
            research.warnings = [
                item for item in research.warnings
                if "Company website not verified" not in item
            ]
        research.sources = list(dict.fromkeys([*research.sources, *(r["url"] for r in company_results)]))[:5]
        risky_terms = ("scam", "fraud", "fake", "complaint")
        if any(term in (r["title"] + " " + r["snippet"]).lower() for r in combined_company_results for term in risky_terms):
            research.warnings.append("Search results mention scam/fraud/fake/complaint terms; verify carefully.")
            research.legitimacy = "Warn only: suspicious terms appeared in search results; do not block automatically."
        else:
            research.legitimacy = "Warn only: web results found; no obvious scam/fake keyword in top snippets."
            research.recommendation = "Proceed only if the Details link or official company site confirms the role and application path."
    else:
        research.warnings.append("No useful web result found for company/recruiter query.")
        research.missing_signals.append("No public company/recruiter result found from the bounded web lookup.")
    if company_salary_sources:
        research.company_salary_sources = company_salary_sources
        research.company_salary_checks = company_salary_check_labels(str(company))
        research.verified_signals.append("Company-specific compensation evidence found.")
        first_company_salary = company_salary_sources[0]
        research.salary_range = f"Company-specific: {first_company_salary['snippet'][:180]}"
        research.sources = list(dict.fromkeys([*research.sources, *(r["url"] for r in company_salary_sources)]))[:8]
    else:
        research.company_salary_checks = company_salary_check_labels(str(company))
        research.missing_signals.append("No company-specific salary range found.")
    return research


def estimate_salary_range(job: dict[str, Any]) -> str:
    """Return a conservative salary note for the role.

    We keep this intentionally brief and transparent. Live research can replace
    or augment this text, but callbacks need a no-network fallback.
    """
    if (job.get("salary") or "").strip():
        return f"Published salary: {job['salary']}. Target ask: {target_salary_label()}."
    title = (job.get("title") or "").lower()
    tech = (job.get("tech_required") or "").lower()
    exp = job.get("min_experience") or -1
    if any(term in title for term in ("lead", "principal", "architect")) or (isinstance(exp, int) and exp >= 6):
        band = "AED 22k–30k/month market ask band"
    elif any(term in tech for term in ("java", "spring", "golang", "microservices", "kubernetes", "aws", "azure")):
        band = "AED 15k–22k/month likely band; higher if backend/cloud ownership is real"
    else:
        band = "AED 12k–18k/month broad Dubai software range"
    return f"{band}. Configured target: {target_salary_label()}."


def build_default_research(job: dict[str, Any]) -> JobResearch:
    company = job.get("company") or "the company"
    website = (job.get("company_website") or "").strip()
    description = str(job.get("description") or "")
    description_summary = _company_summary_from_job_description(str(company), description)
    credibility = str(job.get("credibility_notes") or "").strip()
    is_aggregator = "aggregator" in credibility.lower() or "agency" in credibility.lower()

    summary_bits: list[str] = []
    verified_signals: list[str] = []
    missing_signals: list[str] = []
    warnings: list[str] = []

    if website:
        summary_bits.append(f"{company} has a stored website: {website}.")
        verified_signals.append(f"Stored company website: {website}.")
    else:
        if description_summary:
            summary_bits.append(f"Job post: {description_summary}")
            verified_signals.append("Employer description is present in the job post.")
        else:
            summary_bits.append("No independent company evidence found yet from stored data or bounded lookup.")
        missing_signals.append("Official company website/careers page not confirmed.")
        warnings.append("Company website not verified.")

    if description:
        if not description_summary:
            summary_bits.append("Stored job description is available for stack/scope review.")
        verified_signals.append("Job description text is available.")
    else:
        missing_signals.append("No job description available for stack/scope review.")

    if not (job.get("salary") or "").strip():
        warnings.append("Salary not published.")
        missing_signals.append("Published salary not found.")
    if credibility:
        warnings.append(credibility)
        if is_aggregator:
            missing_signals.append("Direct employer / official application path not confirmed.")

    recruiter = "Not found"
    if (job.get("recruiter_name") or "").strip():
        recruiter = str(job["recruiter_name"])
        if (job.get("recruiter_profile_url") or "").strip():
            recruiter += f" — {job['recruiter_profile_url']}"
        verified_signals.append(f"Recruiter/poster stored: {recruiter}.")
    else:
        missing_signals.append("Recruiter/poster not found.")

    confidence = "Low" if (not website or is_aggregator) else "Medium"
    recommendation = (
        "Low-confidence: verify the employer and official application path before generating documents."
        if confidence == "Low"
        else "Worth continuing if role scope and salary match your target."
    )
    return JobResearch(
        company_summary=" ".join(summary_bits),
        legitimacy="Warn only: missing evidence is not an automatic block, but it should lower confidence until verified.",
        recruiter=recruiter,
        salary_range=estimate_salary_range(job),
        sources=[value for value in [website, job.get("url")] if value],
        warnings=list(dict.fromkeys(warnings)),
        confidence=confidence,
        verified_signals=list(dict.fromkeys(verified_signals)),
        missing_signals=list(dict.fromkeys(missing_signals)),
        recommendation=recommendation,
    )


def _esc(value: Any) -> str:
    return html.escape(str(value or "").strip())


def _looks_like_salary_amount(text: str) -> bool:
    value = text or ""
    amount = r"(?:(?:AED|USD|SAR)\s*\d[\d,]*(?:\.\d+)?[kK]?|[$£€]\s*\d[\d,]*(?:\.\d+)?[kK]?|\d[\d,]*(?:\.\d+)?[kK]?\s*(?:AED|USD|SAR))"
    context = r"(?:salary|salaries|pay|compensation|base|total|range|/month|per month|monthly|/year|per year|yearly|annually)"
    return bool(
        re.search(rf"{context}.{{0,60}}{amount}|{amount}.{{0,60}}{context}", value, flags=re.IGNORECASE)
    )


def _salary_source_matches_job_location(item: dict[str, str], job_location: str) -> bool:
    text = f"{item.get('title') or ''} {item.get('snippet') or ''}".lower()
    location_group = scraper._salary_location_group(job_location)
    if location_group:
        aliases = scraper.SALARY_LOCATION_GROUPS[location_group]
        return any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases)
    city = str(job_location or "").split(",", 1)[0].strip().lower()
    return bool(city and re.search(rf"\b{re.escape(city)}\b", text))


def _compact_benefits(sources: list[dict[str, str]]) -> str:
    text = " ".join(str(item.get("snippet") or "") for item in sources).lower()
    benefits: list[str] = []
    if "bonus" in text:
        benefits.append("bonus")
    if "profit sharing" in text:
        benefits.append("profit sharing")
    if "equity" in text:
        benefits.append("equity")
    if "insurance" in text:
        benefits.append("insurance")
    if not benefits:
        return "Company discusses compensation, but gives no figures."
    return f"{', '.join(benefits)} mentioned; no figures."


def build_research_brief_message(job: dict[str, Any], research: JobResearch) -> str:
    company_name = str(research.employer_name or job.get("company") or "company")
    published_salary = validated_job_salary(job)
    salary_source_priority = {"Levels.fyi": 0, "Glassdoor": 1, "Indeed": 2, "PayScale": 3, "GulfTalent": 4}
    numeric_salary = sorted([
        item for item in research.company_salary_sources
        if item.get("source") != "Company careers page"
        and _looks_like_salary_amount(f"{item.get('title') or ''} {item.get('snippet') or ''}")
        and _salary_source_matches_job_location(item, str(job.get("location") or ""))
    ], key=lambda item: salary_source_priority.get(str(item.get("source") or ""), 99))[:1]
    compensation_notes = [
        item for item in research.company_salary_sources
        if item.get("source") == "Company careers page"
    ]

    if published_salary:
        pay_line = f"Published: {_esc(published_salary)}"
    elif numeric_salary:
        pay_line = " | ".join(
            f"{_esc(item.get('source'))}: {_esc(str(item.get('snippet') or '')[:140])}"
            for item in numeric_salary
        )
    else:
        platforms = ", ".join(COMPANY_PAY_PLATFORMS[:-1]) + f" or {COMPANY_PAY_PLATFORMS[-1]}"
        pay_line = f"No published range; no {_esc(company_name)} pay data on {platforms}."

    benefits_line = _compact_benefits(compensation_notes) if compensation_notes else None

    company_line = " ".join(str(research.company_summary or "Company details not verified.").split())[:180]
    benefits_block = f"\n<b>Benefits:</b> {_esc(benefits_line)}" if benefits_line else ""

    return f"""🔎 <b>Research</b>

<b>{_esc(job.get('title'))}</b>
{_esc(company_name)} — {_esc(job.get('location'))}{f" (via {_esc(research.posting_company)})" if research.posting_company else ""}

<b>Company:</b> {_esc(company_line)}
<b>Pay:</b> {pay_line}{benefits_block}
<b>Ask:</b> Fixed monthly salary and bonus/equity terms.

Choose next step:"""


def research_brief_keyboard(job_id: str, url: str | None = None) -> dict[str, object]:
    row = [
        {"text": "✅ Apply", "callback_data": f"apply:{job_id}"},
        {"text": "🚫 Ignore", "callback_data": f"ignore:{job_id}"},
    ]
    if url:
        row.append({"text": "📄 Details", "url": url})
    return {"inline_keyboard": [row]}


def package_ready_keyboard(job_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "🚀 Proceed to apply", "callback_data": f"proceed_apply:{job_id}"},
                {"text": "⏸ Pause", "callback_data": f"pause:{job_id}"},
            ]
        ]
    }


def tailoring_blocked_keyboard(job_id: str, url: str | None = None) -> dict[str, object]:
    first_row = [
        {"text": "📋 Review required changes", "callback_data": f"resume_refine:{job_id}"},
        {"text": "⏸ Pause", "callback_data": f"pause:{job_id}"},
    ]
    rows: list[list[dict[str, str]]] = [first_row]
    if url:
        rows.append([{"text": "📄 Job details", "url": url}])
    return {"inline_keyboard": rows}


def build_tailoring_blocked_message(job: dict[str, Any], reason: str) -> str:
    return f"""🛑 <b>Resume package paused</b>

<b>{_esc(job.get('title'))}</b>
{_esc(job.get('company'))} — {_esc(job.get('location'))}

{_esc(reason)}

No resume was generated and no application stage was advanced."""


def build_resume_refinement_message(job: dict[str, Any]) -> str:
    return f"""📝 <b>Resume refinement required</b>

<b>{_esc(job.get('title'))}</b> at {_esc(job.get('company'))}

Open JobHunter with the job ID <code>{_esc(job.get('id'))}</code> and complete the Resume Refiner review. Confirm corrected role dates and the complete role-family resume wording before generating another package."""


def build_package_ready_message(job: dict[str, Any], package: ApplicationPackage) -> str:
    return f"""📦 <b>Application package ready</b>

<b>{_esc(job.get('title'))}</b>
{_esc(job.get('company'))} — {_esc(job.get('location'))}

<b>Package:</b> <code>{_esc(package.package_dir)}</code>
<b>Resume:</b> <code>{_esc(package.resume_pdf)}</code>
<b>Cover letter:</b> <code>{_esc(package.cover_pdf)}</code>

Next step requires explicit approval. Proceed to application prep/apply flow?"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    scraper.init_application_tracking(conn)
    return conn


def fetch_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"Job not found: {job_id}")
    return dict(row)


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return value[:70] or "job"


def _load_profile(profile_path: Path | str) -> dict[str, Any]:
    path = Path(profile_path)
    if not path.exists():
        raise FileNotFoundError(f"Candidate profile not found: {path}")
    profile = json.loads(path.read_text(encoding="utf-8"))
    validate_profile(profile)
    return profile


def _normalized_relevance_text(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#./-]+", " ", str(text or "").lower()).split())


def _contains_relevance_term(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _relevance_score(text: Any, job_text: str) -> int:
    candidate = _normalized_relevance_text(text)
    if not candidate:
        return 0
    candidate_words = {
        word for word in re.findall(r"[a-z0-9+#.]+", candidate)
        if len(word) > 2 and word not in _KEYWORD_STOPWORDS
    }
    job_words = {
        word for word in re.findall(r"[a-z0-9+#.]+", job_text)
        if len(word) > 2 and word not in _KEYWORD_STOPWORDS
    }
    score = len(candidate_words & job_words) * 2
    for group in _RELEVANCE_GROUPS:
        if any(_contains_relevance_term(job_text, term) for term in group) and any(
            _contains_relevance_term(candidate, term) for term in group
        ):
            score += 4
    return score


def _job_relevance_text(job: dict[str, Any]) -> str:
    return _normalized_relevance_text(
        " ".join(
            str(job.get(field) or "")
            for field in ("title", "description", "tech_required", "tech_nice_to_have")
        )
    )


def _ranked_values(values: list[Any], job_text: str) -> list[Any]:
    return [
        value
        for _, value in sorted(
            enumerate(values),
            key=lambda item: (-_relevance_score(item[1], job_text), item[0]),
        )
    ]


def _skill_families(skill: Any, job_text: str) -> set[str]:
    candidate = _normalized_relevance_text(skill)
    return {
        family
        for family, job_terms, candidate_terms, _ in _SKILL_SIGNALS
        if any(_contains_relevance_term(job_text, term) for term in job_terms)
        and any(_contains_relevance_term(candidate, term) for term in candidate_terms)
    }


def _skill_relevance_score(skill: Any, job_text: str) -> int:
    candidate = _normalized_relevance_text(skill)
    score = _relevance_score(skill, job_text)
    if candidate and _contains_relevance_term(job_text, candidate):
        score += 10
    for _, job_terms, candidate_terms, bonus in _SKILL_SIGNALS:
        if any(_contains_relevance_term(job_text, term) for term in job_terms) and any(
            _contains_relevance_term(candidate, term) for term in candidate_terms
        ):
            score += bonus
    return score


def _ranked_skills(values: list[Any], job_text: str) -> list[Any]:
    return [
        value
        for _, value in sorted(
            enumerate(values),
            key=lambda item: (-_skill_relevance_score(item[1], job_text), item[0]),
        )
    ]


def _skill_category_score(category: str, values: Any, job_text: str) -> int:
    value_scores = (
        [_skill_relevance_score(value, job_text) for value in values]
        if isinstance(values, list)
        else [_relevance_score(values, job_text)]
    )
    return max(value_scores or [0]) + (_relevance_score(category, job_text) * 2)


def _focused_summary(summary: str, job_text: str, *, limit: int = 3) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", summary.strip()) if part.strip()]
    if len(sentences) <= limit:
        return summary
    ranked_indexes = sorted(
        range(len(sentences)),
        key=lambda index: (-_relevance_score(sentences[index], job_text), index),
    )[:limit]
    return " ".join(sentences[index] for index in sorted(ranked_indexes))


def _experience_role_text(experience: dict[str, Any]) -> str:
    # Use only renderer-allowlisted fields here. Nested engagements may contain
    # draft/private interview material and must not influence public ordering.
    values = (experience.get("title"), experience.get("subtitle"), experience.get("tech"))
    return " ".join(str(value) for value in values if value)


def _experience_relevance_score(
    experience: dict[str, Any],
    job_text: str,
    job_title: str,
) -> int:
    role_text = _experience_role_text(experience)
    evidence_text = " ".join(
        (
            role_text,
            " ".join(str(bullet) for bullet in experience.get("bullets") or []),
        )
    )
    score = _relevance_score(evidence_text, job_text)
    normalized_title = _normalized_relevance_text(job_title)
    normalized_roles = _normalized_relevance_text(role_text)
    title_terms = {
        term
        for term in re.findall(r"[a-z0-9+#.]+", normalized_title)
        if len(term) > 2 and term not in _KEYWORD_STOPWORDS
    }
    role_terms = {
        term
        for term in re.findall(r"[a-z0-9+#.]+", normalized_roles)
        if len(term) > 2 and term not in _KEYWORD_STOPWORDS
    }
    score += len(title_terms & role_terms) * 12
    if _contains_relevance_term(normalized_title, "architect") and _contains_relevance_term(
        normalized_roles, "architect"
    ):
        score += 30
    return score


def _tailored_experience(
    profile: dict[str, Any],
    job_text: str,
    job_title: str = "",
) -> list[dict[str, Any]]:
    projected = project_public_resume(profile).get("experience") or []
    resume_evidence: dict[str, list[str]] = {}
    for item in usable_evidence(profile, "resume"):
        resume_evidence.setdefault(item["experience_id"], []).append(item["public_text"])

    ranked_sources = sorted(
        enumerate(profile.get("experience") or []),
        key=lambda item: (
            -_experience_relevance_score(item[1], job_text, job_title),
            item[0],
        ),
    )
    tailored_experience: list[dict[str, Any]] = []
    for ranked_index, (source_index, source) in enumerate(ranked_sources):
        experience = json.loads(json.dumps(projected[source_index]))
        bullets = list(experience.get("bullets") or [])
        bullets.extend(resume_evidence.get(source.get("id"), []))
        bullets = list(dict.fromkeys(bullets))
        limit = 4 if ranked_index < 3 else 2
        experience["bullets"] = _ranked_values(bullets, job_text)[:limit]
        tailored_experience.append(experience)
    return tailored_experience


def _resume_for_job(
    profile: dict[str, Any],
    job: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return a credible resume payload without exposing tailoring/generation metadata.

    The resume may be selected/ordered for a job internally, but the document itself
    must read like a normal candidate resume. Never mention that it is tailored,
    generated, or built for a specific job/company in the resume content.
    """
    validate_profile(profile)
    job_text = _job_relevance_text(job)
    job_title = str(job.get("title") or "")
    variant = select_resume_variant(profile, job_text, job_title=job_title)
    if variant is not None:
        return apply_resume_variant(profile, variant), variant

    tailored = project_public_resume(profile)
    if profile.get("summary"):
        tailored["summary"] = _focused_summary(str(profile["summary"]), job_text)
    skills = profile.get("skills") or {}
    tailored["skills"] = {
        category: _ranked_skills(list(values), job_text) if isinstance(values, list) else values
        for _, (category, values) in sorted(
            enumerate(skills.items()),
            key=lambda item: (-_skill_category_score(item[1][0], item[1][1], job_text), item[0]),
        )
    }
    tailored["experience"] = _tailored_experience(profile, job_text, job_title)
    return tailored, None


def _tailor_resume(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Return only the renderer-safe resume payload for compatibility."""
    return _resume_for_job(profile, job)[0]


_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_NUMBERS = {
    alias: index
    for index, name in enumerate(_MONTH_NAMES, start=1)
    for alias in (name.lower(), name[:3].lower())
}
_MONTH_NUMBERS["sept"] = 9
_MONTH_YEAR_PATTERN = re.compile(
    rf"\b({'|'.join(sorted(_MONTH_NUMBERS, key=len, reverse=True))})\.?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
_YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")
_CURRENT_DATE_PATTERN = re.compile(r"\b(?:present|current)\b", re.IGNORECASE)
_ARCHITECT_ROLE_PATTERN = re.compile(r"\barchitect(?:ure)?\b", re.IGNORECASE)


def _start_month(value: Any) -> tuple[int, int] | None:
    text = str(value or "")
    month_match = _MONTH_YEAR_PATTERN.search(text)
    if month_match:
        return int(month_match.group(2)), _MONTH_NUMBERS[month_match.group(1).lower()]
    year_match = _YEAR_PATTERN.search(text)
    if year_match:
        return int(year_match.group(1)), 1
    return None


def _title_seniority(title: Any) -> int:
    normalized = _normalized_relevance_text(title)
    if any(
        _contains_relevance_term(normalized, term)
        for term in ("chief technology officer", "cto", "founder")
    ):
        return 5
    if any(
        _contains_relevance_term(normalized, term)
        for term in ("lead", "principal", "architect")
    ):
        return 4
    if _contains_relevance_term(normalized, "senior"):
        return 3
    return 1


def _profile_timeline_issues(profile: dict[str, Any]) -> list[str]:
    """Return privacy-safe issue codes for obvious same-employer progression conflicts."""
    by_company: dict[str, list[dict[str, Any]]] = {}
    for experience in profile.get("experience") or []:
        company = _normalized_relevance_text(experience.get("company"))
        if company:
            by_company.setdefault(company, []).append(experience)

    issues: list[str] = []
    for experiences in by_company.values():
        for current in experiences:
            if not _CURRENT_DATE_PATTERN.search(str(current.get("dates") or "")):
                continue
            if current.get("allow_parallel") is True:
                continue
            current_start = _start_month(current.get("dates"))
            if current_start is None:
                continue
            for later in experiences:
                if later is current or later.get("allow_parallel") is True:
                    continue
                if _CURRENT_DATE_PATTERN.search(str(later.get("dates") or "")):
                    continue
                later_start = _start_month(later.get("dates"))
                if (
                    later_start is not None
                    and later_start > current_start
                    and _title_seniority(later.get("title")) > _title_seniority(current.get("title"))
                ):
                    issues.append("same_employer_progression_still_present")
                    break
    return sorted(set(issues))


def _requires_confirmed_role_variant(job: dict[str, Any]) -> bool:
    return bool(_ARCHITECT_ROLE_PATTERN.search(str(job.get("title") or "")))


def _role_variant_requirement_satisfied(
    job: dict[str, Any],
    selected_variant: dict[str, Any] | None,
) -> bool:
    if not _requires_confirmed_role_variant(job):
        return True
    return bool(selected_variant and selected_variant.get("role_terms"))


def _assert_tailoring_ready(
    profile: dict[str, Any],
    job: dict[str, Any],
    selected_variant: dict[str, Any] | None,
) -> None:
    if _profile_timeline_issues(profile):
        raise TailoringReadinessError(
            "The candidate profile has an inconsistent same-employer role progression. "
            "Confirm the exact role end date before generating another package."
        )
    if not _role_variant_requirement_satisfied(job, selected_variant):
        raise TailoringReadinessError(
            "No candidate-confirmed resume variant matches this architecture role. "
            "Complete the Software Architect resume variant before generating the package."
        )


def _ranked_evidence(
    profile: dict[str, Any],
    job_text: str,
    job_title: str = "",
    *,
    limit: int = 3,
    preserve_experience_order: bool = False,
) -> list[dict[str, str]]:
    evidence: list[tuple[int, int, int, dict[str, str]]] = []
    seen_text: set[str] = set()
    title_words = set(re.findall(r"[a-z]+", job_title.lower())) - {"backend", "office", "intelligence"}
    experience_indexes: dict[str, int] = {}
    experience_contexts: dict[str, str] = {}
    for experience_index, experience in enumerate(profile.get("experience") or []):
        experience_title = str(experience.get("title") or "")
        experience_title_words = set(re.findall(r"[a-z]+", experience_title.lower()))
        title_score = len(title_words & experience_title_words) * 4
        recency_score = max(0, 4 - experience_index)
        context = " - ".join(
            value for value in (experience_title, str(experience.get("company") or "")) if value
        )
        experience_id = experience.get("id")
        if isinstance(experience_id, str):
            experience_indexes[experience_id] = experience_index
            experience_contexts[experience_id] = context
        for bullet_index, bullet in enumerate(experience.get("bullets") or []):
            text = str(bullet).strip()
            if text in seen_text:
                continue
            seen_text.add(text)
            evidence.append(
                (
                    _relevance_score(bullet, job_text) + title_score + recency_score,
                    experience_index,
                    bullet_index,
                    {"text": text, "context": context},
                )
            )
    for evidence_index, item in enumerate(usable_evidence(profile, "cover-letter")):
        text = item["public_text"]
        if text in seen_text:
            continue
        seen_text.add(text)
        experience_id = item["experience_id"]
        experience_index = experience_indexes[experience_id]
        experience = profile["experience"][experience_index]
        experience_title = str(experience.get("title") or "")
        experience_title_words = set(re.findall(r"[a-z]+", experience_title.lower()))
        title_score = len(title_words & experience_title_words) * 4
        recency_score = max(0, 4 - experience_index)
        evidence.append(
            (
                _relevance_score(text, job_text) + title_score + recency_score,
                experience_index,
                len(experience.get("bullets") or []) + evidence_index,
                {"text": text, "context": experience_contexts[experience_id]},
            )
        )
    if preserve_experience_order:
        evidence.sort(key=lambda item: (item[1], item[2]))
    else:
        evidence.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in evidence[:limit]]


def _matched_skills(profile: dict[str, Any], job_text: str, *, limit: int = 5) -> list[str]:
    values = [
        str(skill)
        for skills in (profile.get("skills") or {}).values()
        for skill in (skills if isinstance(skills, list) else [skills])
    ]
    ranked = [value for value in _ranked_skills(values, job_text) if _skill_relevance_score(value, job_text) > 0]
    selected: list[str] = []
    covered_families: set[str] = set()
    for value in ranked:
        families = _skill_families(value, job_text)
        if selected and families and families <= covered_families:
            continue
        selected.append(value)
        covered_families.update(families)
        if len(selected) >= limit:
            return selected
    for value in ranked:
        if value not in selected:
            selected.append(value)
        if len(selected) >= limit:
            break
    return selected


def _role_focus(job_text: str, *, limit: int = 3) -> list[str]:
    signals = (
        (("server-side", "backend"), "scalable backend services"),
        (("distributed system", "message queue", "distributed storage"), "distributed systems"),
        (("backend infrastructure", "infrastructure"), "backend infrastructure"),
        (("high-performance", "performance", "scalable"), "performance and reliability"),
        (("mysql", "nosql", "database"), "data-intensive systems"),
        (("ai / ml", "ai/ml", "machine learning"), "applied AI"),
    )
    return [
        label for terms, label in signals
        if any(_contains_relevance_term(job_text, term) for term in terms)
    ][:limit]


def _natural_join(values: list[str]) -> str:
    if len(values) < 2:
        return values[0] if values else ""
    if len(values) == 2:
        return " and ".join(values)
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _cover_letter(
    profile: dict[str, Any],
    job: dict[str, Any],
    *,
    preserve_experience_order: bool = False,
) -> dict[str, Any]:
    name = str(profile["name"])
    contact_parts = [profile.get("email"), profile.get("phone"), profile.get("linkedin")]
    company = str(job.get("company") or "the company")
    title = str(job.get("title") or "Software Engineer")
    job_text = _job_relevance_text(job)
    skills = _matched_skills(profile, job_text)
    focus = _role_focus(job_text)
    years_match = re.search(r"\b\d+\+?\s+years\b", str(profile.get("summary") or ""), re.IGNORECASE)
    opening_sentences = [f"I am applying for the {title} role at {company}."]
    if skills:
        if years_match:
            opening_sentences.append(
                f"With {years_match.group(0)} of experience, my background includes {_natural_join(skills)}."
            )
        else:
            opening_sentences.append(f"My background includes {_natural_join(skills)}.")
    if focus:
        opening_sentences.append(
            f"This background is relevant to the role's focus on {_natural_join(focus)}."
        )
    team_name = title.split(",", 1)[1].strip() if "," in title else title
    now = datetime.now(timezone.utc)
    return {
        "name": name,
        "contact": " | ".join(str(p) for p in contact_parts if p),
        "date": f"{now.strftime('%B')} {now.day}, {now.year}",
        "recipient": f"{company} Hiring Team",
        "subject": f"Application for {title}",
        "salutation": "Dear Hiring Team,",
        "opening": " ".join(opening_sentences),
        "highlights_heading": "Relevant examples from my experience include:",
        "highlights": _ranked_evidence(
            profile,
            job_text,
            title,
            preserve_experience_order=preserve_experience_order,
        ),
        "motivation": (
            f"I am particularly interested in contributing to the {team_name} team, where the role combines "
            "system design, production delivery, and continuous technical improvement."
        ),
        "closing": (
            f"I would welcome the opportunity to discuss how my experience could contribute to {company}. "
            f"Thank you for your consideration."
        ),
        "signoff": "Sincerely,",
        "signature": name,
        "paragraphs": [
            # Kept for compatibility with any downstream consumer of the JSON payload.
            f"I am applying for the {title} role at {company}.",
        ],
    }


def _project_python() -> str:
    root = Path(__file__).resolve().parent
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _render_pdf(mode: str, input_path: Path, output_path: Path) -> int:
    proc = subprocess.run(
        [_project_python(), "render_pdf.py", mode, str(input_path), str(output_path)],
        cwd=Path(__file__).resolve().parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "PDF render failed").strip().splitlines()[-1]
        raise RuntimeError(f"PDF render failed for {mode}: {detail}")
    for line in reversed(proc.stdout.splitlines()):
        try:
            metadata = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(metadata, dict)
            and metadata.get("jobhunter_pdf_render") == 1
            and metadata.get("mode") == mode
            and isinstance(metadata.get("pages"), int)
            and not isinstance(metadata["pages"], bool)
            and metadata["pages"] > 0
        ):
            return metadata["pages"]
    raise RuntimeError(f"PDF renderer did not report a valid page count for {mode}")


def _enforce_resume_page_limit(page_count: int, max_pages: Any) -> None:
    if max_pages is None:
        return
    if page_count > max_pages:
        raise RuntimeError(
            f"Generated resume is {page_count} pages; the confirmed variant allows at most {max_pages}"
        )


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def _make_private(path: Path) -> None:
    if path.exists():
        path.chmod(0o600)


def _profile_digest(profile: dict[str, Any]) -> str:
    canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _remove_generated_path(path: Path | None) -> None:
    if path is None or (not path.exists() and not path.is_symlink()):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _assert_direct_output_child(path: Path, output_root: Path) -> None:
    if path.parent.resolve() != output_root.resolve():
        raise ValueError("Package paths must remain direct children of the configured output directory")


def _promote_package_directory(
    staging_dir: Path,
    package_dir: Path,
    output_root: Path,
) -> Path | None:
    """Atomically promote a complete staged package, retaining the old one for rollback."""
    _assert_direct_output_child(staging_dir, output_root)
    _assert_direct_output_child(package_dir, output_root)
    backup_dir: Path | None = None
    if package_dir.exists() or package_dir.is_symlink():
        backup_dir = package_dir.with_name(f".{package_dir.name}.backup-{uuid.uuid4().hex}")
        _assert_direct_output_child(backup_dir, output_root)
        os.replace(package_dir, backup_dir)
    try:
        os.replace(staging_dir, package_dir)
    except Exception:
        if backup_dir is not None:
            os.replace(backup_dir, package_dir)
        raise
    return backup_dir


def _restore_previous_package(
    package_dir: Path,
    backup_dir: Path | None,
    output_root: Path,
) -> None:
    _assert_direct_output_child(package_dir, output_root)
    if backup_dir is not None:
        _assert_direct_output_child(backup_dir, output_root)
    _remove_generated_path(package_dir)
    if backup_dir is not None and (backup_dir.exists() or backup_dir.is_symlink()):
        os.replace(backup_dir, package_dir)


def prepare_application_package(
    job_id: str,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    profile_path: Path | str = DEFAULT_PROFILE_PATH,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    render_pdfs: bool = True,
) -> ApplicationPackage:
    conn = _connect(db_path)
    staging_dir: Path | None = None
    package_dir: Path | None = None
    backup_dir: Path | None = None
    output_root: Path | None = None
    promoted = False
    committed = False
    try:
        job = fetch_job(conn, job_id)
        profile = _load_profile(profile_path)
        resume_payload, selected_variant = _resume_for_job(profile, job)
        _assert_tailoring_ready(profile, job, selected_variant)
        safe_job_id = str(job_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", safe_job_id):
            raise ValueError("job_id contains unsafe path characters")
        output_root = Path(output_dir).resolve()
        if output_root == Path(output_root.anchor):
            raise ValueError("The filesystem root cannot be used as the package output directory")
        package_dir = output_root / f"{safe_job_id}-{_slug(job.get('company') or '')}-{_slug(job.get('title') or '')}"
        _assert_direct_output_child(package_dir, output_root)
        cover_source = resume_payload if selected_variant is not None else profile
        variant_max_pages = selected_variant.get("max_pages") if selected_variant is not None else None
        if not render_pdfs and variant_max_pages is not None:
            raise RuntimeError("A page-limited confirmed resume variant requires PDF rendering")

        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{package_dir.name}.staging-",
                dir=output_root,
            )
        )
        _assert_direct_output_child(staging_dir, output_root)
        staging_dir.chmod(0o700)
        staged_resume_json = staging_dir / "resume.json"
        staged_cover_json = staging_dir / "cover_letter.json"
        staged_manifest_json = staging_dir / "tailoring_manifest.json"
        staged_resume_pdf = staging_dir / "Resume.pdf"
        staged_cover_pdf = staging_dir / "CoverLetter.pdf"

        _write_private_json(staged_resume_json, resume_payload)
        _write_private_json(
            staged_cover_json,
            _cover_letter(
                cover_source,
                job,
                preserve_experience_order=selected_variant is not None,
            ),
        )
        resume_page_count: int | None = None
        if render_pdfs:
            resume_page_count = _render_pdf("resume", staged_resume_json, staged_resume_pdf)
            _enforce_resume_page_limit(
                resume_page_count,
                variant_max_pages,
            )
            _render_pdf("cover", staged_cover_json, staged_cover_pdf)
        else:
            staged_resume_pdf.write_text("PDF rendering skipped in test mode", encoding="utf-8")
            staged_cover_pdf.write_text("PDF rendering skipped in test mode", encoding="utf-8")
        _make_private(staged_resume_pdf)
        _make_private(staged_cover_pdf)
        _write_private_json(
            staged_manifest_json,
            {
                "manifest_version": 1,
                "job_id": job_id,
                "job_title": str(job.get("title") or ""),
                "tailoring_mode": "confirmed_variant" if selected_variant is not None else "legacy_fallback",
                "selected_variant_id": selected_variant.get("id") if selected_variant is not None else None,
                "profile_sha256": _profile_digest(profile),
                "resume_pages": resume_page_count,
                "quality_checks": {
                    "timeline_consistent": True,
                    "role_variant_required": _requires_confirmed_role_variant(job),
                    "role_variant_requirement_satisfied": _role_variant_requirement_satisfied(
                        job,
                        selected_variant,
                    ),
                },
            },
        )

        backup_dir = _promote_package_directory(staging_dir, package_dir, output_root)
        staging_dir = None
        promoted = True
        resume_json = package_dir / "resume.json"
        cover_json = package_dir / "cover_letter.json"
        manifest_json = package_dir / "tailoring_manifest.json"
        resume_pdf = package_dir / "Resume.pdf"
        cover_pdf = package_dir / "CoverLetter.pdf"
        package = ApplicationPackage(
            job_id,
            package_dir,
            resume_json,
            cover_json,
            resume_pdf,
            cover_pdf,
            manifest_json,
        )
        scraper.record_application_stage(
            conn,
            job_id,
            "package_generated",
            package_path=str(package_dir),
            platform=job.get("source"),
            application_type="linkedin_unknown" if "linkedin" in (job.get("url") or "").lower() else "external_unknown",
            application_url=job.get("url"),
            notes="Resume and cover letter generated; awaiting explicit Proceed to apply approval.",
            commit=False,
            sync=False,
        )
        conn.commit()
        committed = True
    except Exception:
        conn.rollback()
        if promoted and not committed and package_dir is not None and output_root is not None:
            _restore_previous_package(package_dir, backup_dir, output_root)
            backup_dir = None
        raise
    finally:
        if staging_dir is not None and output_root is not None:
            _assert_direct_output_child(staging_dir, output_root)
        _remove_generated_path(staging_dir)
        conn.close()

    if backup_dir is not None and output_root is not None:
        _assert_direct_output_child(backup_dir, output_root)
    _remove_generated_path(backup_dir)
    try:
        scraper.sync_application_tracker_if_enabled()
    except Exception:
        pass
    return package


def render_research_dry_run(job_id: str, *, db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """Run live research without sending Telegram or mutating application state."""
    resolved_db = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{resolved_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"Job not found: {job_id}")
    job = dict(row)
    return build_research_brief_message(job, research_job(job))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview Interested-stage research without sending Telegram.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--db-path", default="data/jobs.db")
    args = parser.parse_args(argv)

    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")
    try:
        print(render_research_dry_run(args.job_id, db_path=args.db_path))
    except KeyError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
