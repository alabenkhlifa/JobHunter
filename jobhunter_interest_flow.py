"""Interested-job research and application-package helpers for JobHunter.

This module is intentionally deterministic and testable. Live web research can
feed a JobResearch object, but the formatting/state transitions here avoid
network calls so Telegram callbacks remain reliable.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import scraper

DEFAULT_TARGET_SALARY_AED_MONTHLY = 30000


@dataclass
class JobResearch:
    company_summary: str
    legitimacy: str
    recruiter: str = "Not found"
    salary_range: str = "Salary not published; use configured target as anchor."
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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


def web_search_results(query: str, *, timeout: float | None = None, fetcher=requests.get) -> list[dict[str, str]]:
    timeout = timeout if timeout is not None else float(os.getenv("JOBHUNTER_WEB_RESEARCH_TIMEOUT", "8"))
    response = fetcher(
        "https://duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": "Mozilla/5.0 JobHunter research bot"},
        timeout=timeout,
    )
    if response.status_code != 200:
        return []
    return parse_duckduckgo_results(response.text)


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
        company_results = web_search_results(f"{company} company {location} {title} recruiter")[:3]
        salary_results = web_search_results(f"{location} {title} salary AED monthly")[:3]
    except Exception as exc:  # noqa: BLE001 - callback must stay reliable
        research.warnings.append(f"Web research unavailable: {exc.__class__.__name__}.")
        return research
    if company_results:
        top = company_results[0]
        research.company_summary = f"Top web result: {top['title']} — {top['snippet'][:180]}"
        research.sources = list(dict.fromkeys([*research.sources, *(r["url"] for r in company_results)]))[:5]
        risky_terms = ("scam", "fraud", "fake", "complaint")
        if any(term in (r["title"] + " " + r["snippet"]).lower() for r in company_results for term in risky_terms):
            research.warnings.append("Search results mention scam/fraud/fake/complaint terms; verify carefully.")
            research.legitimacy = "Warn only: suspicious terms appeared in search results; do not block automatically."
        else:
            research.legitimacy = "Warn only: web results found; no obvious scam/fake keyword in top snippets."
    else:
        research.warnings.append("No useful web result found for company/recruiter query.")
    if salary_results:
        research.salary_range = f"{research.salary_range} Salary search context: {salary_results[0]['title']} — {salary_results[0]['snippet'][:140]}"
        research.sources = list(dict.fromkeys([*research.sources, *(r["url"] for r in salary_results)]))[:5]
    else:
        research.warnings.append("No useful salary web result found.")
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
    summary_bits = []
    if website:
        summary_bits.append(f"{company} has a listed website: {website}.")
    else:
        summary_bits.append(f"{company} needs a quick public web/company-page check before investing time.")
    if job.get("description"):
        summary_bits.append("The job description is specific enough to review stack and scope.")
    warnings = []
    if not (job.get("salary") or "").strip():
        warnings.append("Salary not published.")
    if not website:
        warnings.append("Company website not stored yet.")
    if (job.get("credibility_notes") or "").strip():
        warnings.append(str(job["credibility_notes"]))
    recruiter = "Not found"
    if (job.get("recruiter_name") or "").strip():
        recruiter = str(job["recruiter_name"])
        if (job.get("recruiter_profile_url") or "").strip():
            recruiter += f" — {job['recruiter_profile_url']}"
    return JobResearch(
        company_summary=" ".join(summary_bits),
        legitimacy="Warn only: no automatic block. Verify salary, contract, and official application path before applying.",
        recruiter=recruiter,
        salary_range=estimate_salary_range(job),
        sources=[value for value in [website, job.get("url")] if value],
        warnings=warnings,
    )


def _esc(value: Any) -> str:
    return html.escape(str(value or "").strip())


def build_research_brief_message(job: dict[str, Any], research: JobResearch) -> str:
    warnings = research.warnings or ["No major warning captured yet."]
    sources = research.sources[:3]
    warning_lines = "\n".join(f"• {_esc(w)}" for w in warnings)
    source_lines = "\n".join(f"• {_esc(s)}" for s in sources) if sources else "• Not captured"
    return f"""🔎 <b>Research brief</b>

<b>{_esc(job.get('title'))}</b>
{_esc(job.get('company'))} — {_esc(job.get('location'))}

<b>Company:</b> {_esc(research.company_summary)}
<b>Legit/fake check:</b> {_esc(research.legitimacy)}
<b>Recruiter:</b> {_esc(research.recruiter)}
<b>Salary:</b> {_esc(research.salary_range)}
<b>Your target:</b> {_esc(target_salary_label())}

<b>Warnings — Warn only:</b>
{warning_lines}

<b>Sources:</b>
{source_lines}

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
