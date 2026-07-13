"""Interested-job research and application-package helpers for JobHunter.

This module is intentionally deterministic and testable. Live web research can
feed a JobResearch object, but the formatting/state transitions here avoid
network calls so Telegram callbacks remain reliable.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from typing import Any

import scraper

DEFAULT_TARGET_SALARY_AED_MONTHLY = 30000
COMPANY_PAY_PLATFORMS = ("Glassdoor", "Indeed", "PayScale", "GulfTalent", "Levels.fyi")


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


@dataclass
class ApplicationPackage:
    job_id: str
    package_dir: Path
    resume_json: Path
    cover_json: Path
    resume_pdf: Path
    cover_pdf: Path


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


def company_salary_search_queries(company: str, title: str, location: str) -> list[str]:
    company = " ".join(str(company or "").split())
    if not company:
        return []
    return [
        f'"{company}" careers compensation salary benefits',
        f'site:glassdoor.com/Salary "{company}"',
        f'site:indeed.com/cmp "{company}" salaries',
        f'site:payscale.com "{company}" salary',
        f'site:gulftalent.com "{company}" salary',
        f'site:levels.fyi/companies "{company}"',
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
    if "salaryexpert" in host:
        return "SalaryExpert"
    return host or title.split(" - ")[0]


def _company_identity_token(company: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(company or "").lower())
    legal_suffixes = {"fz", "fze", "llc", "ltd", "limited", "inc", "corp", "company"}
    while words and words[-1] in legal_suffixes:
        words.pop()
    return "".join(words)


def _result_matches_company(company: str, result: dict[str, str]) -> bool:
    """Require the employer identity in the title or URL, never only the snippet."""
    company_token = _company_identity_token(company)
    if len(company_token) < 4:
        return False
    title_token = re.sub(r"[^a-z0-9]", "", str(result.get("title") or "").lower())
    parsed = urlparse(str(result.get("url") or ""))
    url_token = re.sub(r"[^a-z0-9]", "", f"{parsed.netloc}{parsed.path}".lower())
    return company_token in title_token or company_token in url_token


def _is_official_company_result(company: str, url: str) -> bool:
    company_token = _company_identity_token(company)
    host_token = re.sub(r"[^a-z0-9]", "", urlparse(str(url or "")).netloc.lower().removeprefix("www."))
    blocked_hosts = ("glassdoor", "indeed", "payscale", "gulftalent", "levels", "linkedin")
    return bool(company_token and company_token in host_token and not any(host in host_token for host in blocked_hosts))


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
    if any(term in lowered for term in ("technology consultancy", "technology consulting", "tech consultancy")):
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
    if "cloud" in lowered:
        focus.append("cloud platforms")
    if any(term in lowered for term in ("artificial intelligence", " ai ", "machine learning")):
        focus.append("AI")

    base = f"{company} is a {city + '-based ' if city else ''}{company_type}"
    if focus:
        base += f" focused on {', '.join(dict.fromkeys(focus[:3]))}"
    size_match = re.search(r"\b(\d{1,4})\s*(?:-|–|to)\s*(\d{1,4})\s+employees\b", lowered)
    if size_match:
        base += f" ({size_match.group(1)}–{size_match.group(2)} employees)"
    return base + "."


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
            if not any(term in text for term in ("salary", "salaries", "aed", "pay", "compensation", "bonus", "equity", "profit sharing")):
                continue
            seen_urls.add(url)
            sources.append({
                "source": "Company careers page" if _is_official_company_result(company, url) else _salary_source_name(url, result.get("title", "")),
                "title": result.get("title", ""),
                "url": url,
                "snippet": result.get("snippet", ""),
            })
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
    research = build_default_research(job)
    if not web_research_enabled():
        return research
    company = job.get("company") or ""
    title = job.get("title") or ""
    location = job.get("location") or "Dubai"
    try:
        timeout = float(os.getenv("JOBHUNTER_WEB_RESEARCH_TIMEOUT", "12"))
        profile_queries = company_profile_search_queries(str(company), str(title), str(location))
        with ThreadPoolExecutor(max_workers=len(profile_queries)) as executor:
            profile_result_groups = list(
                executor.map(lambda query: web_search_results(query, timeout=timeout)[:5], profile_queries)
            )
        raw_company_results = [result for group in profile_result_groups for result in group]
        preferred_company_results = [r for r in raw_company_results if _result_matches_company(str(company), r)]
        combined_company_results: list[dict[str, str]] = []
        seen_company_urls: set[str] = set()
        for result in preferred_company_results:
            url = result.get("url", "")
            if url and url not in seen_company_urls:
                seen_company_urls.add(url)
                combined_company_results.append(result)
        company_results = combined_company_results[:3]
        company_salary_sources = collect_company_salary_sources(str(company), str(title), str(location), timeout=timeout)
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
        summary_bits.append("No independent company evidence found yet from stored data or bounded lookup.")
        missing_signals.append("Official company website/careers page not confirmed.")
        warnings.append("Company website not verified.")

    if job.get("description"):
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


def build_research_brief_message(job: dict[str, Any], research: JobResearch) -> str:
    company_name = str(job.get("company") or "company")
    published_salary = str(job.get("salary") or "").strip()
    numeric_salary = [
        item for item in research.company_salary_sources
        if item.get("source") != "Company careers page"
        and _looks_like_salary_amount(f"{item.get('title') or ''} {item.get('snippet') or ''}")
    ][:2]
    compensation_notes = [
        item for item in research.company_salary_sources
        if item.get("source") == "Company careers page"
        and not _looks_like_salary_amount(f"{item.get('title') or ''} {item.get('snippet') or ''}")
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

    benefits_line = None
    if compensation_notes:
        benefits_line = "Bonus, profit sharing and senior-role equity mentioned; no figures."

    company_line = " ".join(str(research.company_summary or "Company details not verified.").split())[:180]
    benefits_block = f"\n<b>Benefits:</b> {_esc(benefits_line)}" if benefits_line else ""

    return f"""🔎 <b>Research</b>

<b>{_esc(job.get('title'))}</b>
{_esc(job.get('company'))} — {_esc(job.get('location'))}

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
                {"text": "⏸ Pause", "callback_data": f"ignore:{job_id}"},
            ]
        ]
    }


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
        return {
            "name": "Ala Ben Khalifa",
            "headline": "Software Architect / Backend Lead",
            "summary": "7+ years building backend, cloud, and software architecture systems.",
            "skills": {"Backend": ["Java", "Spring Boot", "Go", "Microservices"], "Cloud": ["AWS", "Azure", "Docker", "Kubernetes"]},
            "experience": [],
            "education": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _tailor_resume(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    tailored = json.loads(json.dumps(profile))
    title = job.get("title") or "the selected role"
    company = job.get("company") or "the company"
    tech = job.get("tech_required") or "backend, cloud, and distributed systems"
    tailored["headline"] = f"Software Architect / Backend Lead — tailored for {title} at {company}"
    tailored["summary"] = (
        f"7+ years of backend/cloud engineering experience aligned with {title} at {company}. "
        f"Relevant focus: {tech}."
    )
    return tailored


def _cover_letter(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    name = profile.get("name") or "Ala Ben Khalifa"
    contact_parts = [profile.get("email"), profile.get("phone"), profile.get("linkedin")]
    return {
        "name": name,
        "contact": " | ".join(str(p) for p in contact_parts if p),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "recipient": job.get("company") or "Hiring Team",
        "subject": f"Application for {job.get('title') or 'Software Role'}",
        "paragraphs": [
            f"Dear Hiring Team, I am interested in the {job.get('title')} role at {job.get('company')}. The backend/cloud scope of the role is closely aligned with my 7+ years of experience building scalable systems.",
            f"The role's focus on {job.get('tech_required') or 'backend engineering and modern delivery practices'} matches my experience across architecture, APIs, cloud platforms, and reliable delivery.",
            "I would welcome the chance to discuss how I can contribute to the team while keeping compensation expectations aligned early in the process.",
        ],
    }


def _project_python() -> str:
    root = Path(__file__).resolve().parent
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _render_pdf(mode: str, input_path: Path, output_path: Path) -> None:
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


def prepare_application_package(
    job_id: str,
    *,
    db_path: Path | str = "data/jobs.db",
    profile_path: Path | str = "data/master-profile.json",
    output_dir: Path | str = "data/output",
    render_pdfs: bool = True,
) -> ApplicationPackage:
    with _connect(db_path) as conn:
        job = fetch_job(conn, job_id)
        profile = _load_profile(profile_path)
        package_dir = Path(output_dir) / f"{job_id}-{_slug(job.get('company') or '')}-{_slug(job.get('title') or '')}"
        package_dir.mkdir(parents=True, exist_ok=True)
        resume_json = package_dir / "resume.json"
        cover_json = package_dir / "cover_letter.json"
        resume_pdf = package_dir / "Resume_Tailored.pdf"
        cover_pdf = package_dir / "CoverLetter_Tailored.pdf"
        resume_json.write_text(json.dumps(_tailor_resume(profile, job), indent=2, ensure_ascii=False), encoding="utf-8")
        cover_json.write_text(json.dumps(_cover_letter(profile, job), indent=2, ensure_ascii=False), encoding="utf-8")
        if render_pdfs:
            _render_pdf("resume", resume_json, resume_pdf)
            _render_pdf("cover", cover_json, cover_pdf)
        else:
            resume_pdf.write_text("PDF rendering skipped in test mode", encoding="utf-8")
            cover_pdf.write_text("PDF rendering skipped in test mode", encoding="utf-8")
        package = ApplicationPackage(job_id, package_dir, resume_json, cover_json, resume_pdf, cover_pdf)
        scraper.record_application_stage(
            conn,
            job_id,
            "package_generated",
            package_path=str(package_dir),
            platform=job.get("source"),
            application_type="linkedin_unknown" if "linkedin" in (job.get("url") or "").lower() else "external_unknown",
            application_url=job.get("url"),
            notes="Resume and cover letter generated; awaiting explicit Proceed to apply approval.",
        )
        return package


def render_research_dry_run(job_id: str, *, db_path: Path | str = "data/jobs.db") -> str:
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
