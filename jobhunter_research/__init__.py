"""Salary and company research for one job.

``research(job)`` returns a ResearchResult whose ``to_dict()`` is the frozen
contract shape. It runs at most four web queries (market salary, company
salary, company profile) plus two direct salary pages for Spain, caches the
result in ``job_research`` when a connection is given, and never raises for
network failures: they become ``notes``.
"""

from __future__ import annotations

import html
import inspect
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from jobhunter_research import cache, company, roles, salary
from jobhunter_research.salary import ask_from_range
from jobhunter_research.search import BROWSER_HEADERS

RESEARCH_VERSION = 3
FETCH_TIMEOUT = 10
__all__ = ["RESEARCH_VERSION", "ResearchResult", "research", "ask_from_range", "format_salary_lines",
           "default_fetch"]

_COUNTRY_LABELS = {"spain": "Spain", "uae": "UAE", "saudi": "Saudi Arabia", "switzerland": "Switzerland"}
_MARKET_UNITS = {"spain": ("EUR", "year"), "uae": ("AED", "month"), "saudi": ("SAR", "month"),
                 "switzerland": ("CHF", "year")}
# Salary sites spell these in Spanish/English, not Valencian.
_CITY_NAMES = {"valència": "Valencia", "alacant": "Alicante", "castelló": "Castellón"}
_NOT_A_CITY = {"remote", "hybrid", "remoto", "teletrabajo", "anywhere", "spain", "españa", "espana"}


def _flow():
    import jobhunter_interest_flow as flow

    return flow


def default_fetch(url: str, *, timeout: float = FETCH_TIMEOUT, **kwargs: Any):
    """GET with a browser User-Agent; the response object, or raises."""
    headers = {**BROWSER_HEADERS, **(kwargs.pop("headers", None) or {})}
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, **kwargs)


RISK_TERMS = ("scam", "fraud", "fake", "complaint")
RISK_NOTE = "Search results mention scam/fraud/fake/complaint terms; verify carefully."


def _mentions_risk(employer: str, rows: list[dict[str, str]]) -> bool:
    matcher = getattr(_flow(), "_result_matches_company", None)
    for row in rows:
        if matcher is not None and not matcher(str(employer), row):
            continue
        text = f"{row.get('title', '')} {row.get('snippet', '')}".lower()
        if any(term in text for term in RISK_TERMS):
            return True
    return False


@dataclass
class ResearchResult:
    job_id: str
    researched_at: str
    salary: dict[str, Any]
    company: dict[str, Any]
    sources: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    version: int = RESEARCH_VERSION
    # Not serialized: raw search rows (for scam-term checks) and where the
    # company summary came from ("job_post" or "web").
    raw_results: list[dict[str, str]] = field(default_factory=list, repr=False, compare=False)
    summary_origin: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        s, c = self.salary, self.company
        return {
            "job_id": self.job_id,
            "version": self.version,
            "researched_at": self.researched_at,
            "salary": {key: s.get(key) for key in ("low", "high", "currency", "period", "basis", "ask_amount",
                                                    "published_text", "confidence", "ask_basis", "compensation_basis", "proxy_for_level")},
            "company": {key: c.get(key) if key != "sources" else list(c.get("sources") or [])
                        for key in ("name", "website", "summary", "industry", "size", "hq", "sources")},
            "sources": [{**{key: source.get(key, "") for key in ("kind", "name", "url", "snippet", "retrieved_at")},
                         **({"evidence": {key: value for key, value in source["evidence"].items()
                              if key in {"role_family", "level", "geography", "compensation_basis", "statistic", "scope", "proxy_for_level"}
                              and isinstance(value, str)}} if isinstance(source.get("evidence"), dict) else {})}
                        for source in self.sources],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchResult":
        company_data = dict(data["company"])
        origin = ""
        if company_data.get("summary"):
            origin = "job_post" if (company_data.get("sources") or [""])[0] == "job description" else "web"
        return cls(
            job_id=str(data["job_id"]),
            researched_at=str(data["researched_at"]),
            salary=dict(data["salary"]),
            company=company_data,
            sources=[dict(source) for source in data.get("sources") or []],
            notes=list(data.get("notes") or []),
            version=int(data.get("version") or RESEARCH_VERSION),
            summary_origin=origin,
        )


@dataclass
class ResearchContext:
    job: dict[str, Any]
    job_id: str
    title: str
    employer: str
    location: str
    market: str | None
    currency: str | None
    period: str | None
    city: str
    country_label: str
    family: str | None
    level: str
    search_role: str
    base_role: str
    description: str
    company_website: str
    source: str
    about_text: str


def _about_text(name: str, description: str) -> str:
    match = re.search(rf"\bAbout\s+(?:us\b|{re.escape(name)}\b)", description, re.IGNORECASE) if name else None
    if not match:
        return ""
    return description[match.start():match.start() + 800]


def build_context(job: dict[str, Any]) -> ResearchContext:
    flow = _flow()
    employer, _posting = flow.resolve_research_employer(job)
    location = " ".join(str(job.get("location") or "").split())
    market = flow.salary_market(job) if location else None
    currency, period = _MARKET_UNITS.get(market or "", (None, None))
    city = flow._salary_city(location) if location else ""
    if city.strip().lower() in _NOT_A_CITY or (market and city.strip().lower() == market):
        city = ""
    city = city.strip().title() if city.islower() else city.strip()
    city = _CITY_NAMES.get(city.lower(), city)
    title = " ".join(str(job.get("title") or "").split())
    family = roles.role_family(title, job.get("min_experience"))
    level = roles.role_level(title, job.get("min_experience"))
    role_phrase = roles.search_role(title, family, level)
    description = str(job.get("description") or "")
    return ResearchContext(
        job=job, job_id=str(job.get("id") or ""), title=title, employer=employer or str(job.get("company") or ""),
        location=location, market=market, currency=currency, period=period, city=city,
        country_label=_COUNTRY_LABELS.get(market or "", ""), family=family, level=level, search_role=role_phrase,
        base_role=roles.base_role(role_phrase), description=description,
        company_website=str(job.get("company_website") or "").strip(), source=str(job.get("source") or ""),
        about_text=_about_text(employer, description),
    )


def _bind_search(search: Callable | None, notes: list[str]) -> Callable[[str], list[dict[str, str]]]:
    """Wrap a search callable so failures become notes and results stay lists."""
    if search is None:
        flow_search = _flow().web_search_results
        try:
            parameters = inspect.signature(flow_search).parameters
            takes_notes = "notes" in parameters or any(p.kind is p.VAR_KEYWORD for p in parameters.values())
        except (TypeError, ValueError):
            takes_notes = False

        def base(query: str):
            return flow_search(query, notes=notes) if takes_notes else flow_search(query)
    else:
        base = search

    def run(query: str) -> list[dict[str, str]]:
        try:
            rows = base(query) or []
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            notes.append(f"Search failed for {query!r}: {exc.__class__.__name__}.")
            return []
        rows = [row for row in rows if isinstance(row, dict) and row.get("url")]
        if not rows:
            notes.append(f"No search results for {query!r}.")
        return rows

    return run


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def research(job: dict[str, Any], *, conn: sqlite3.Connection | None = None, max_age_days: float = 14,
             search: Callable | None = None, fetch: Callable | None = None, now: datetime | None = None,
             quantile: float = 0.75, round_to: int = 1000) -> ResearchResult:
    """Salary + company research for ``job``, from the cache when fresh."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    job_id = str(job.get("id") or "")
    notes: list[str] = []
    context_sha256 = cache.context_digest(job, {"target": _flow().salary_target(job),
                                               "quantile": quantile, "round_to": round_to})
    if conn is not None and job_id:
        try:
            cache.ensure_schema(conn)
            cached = cache.load(conn, job_id, max_age_days, now=now, context_sha256=context_sha256)
        except sqlite3.Error as exc:
            notes.append(f"Research cache unavailable: {exc.__class__.__name__}.")
            cached = None
        if cached is not None:
            return cached

    ctx = build_context(job)
    run_search = _bind_search(search, notes)
    fetch = fetch or default_fetch
    now_iso = _now_iso(now)
    published, published_source = salary.published_salary(ctx, now_iso)

    company_query = salary.company_query(ctx)
    with ThreadPoolExecutor(max_workers=5) as pool:
        market_rows = pool.submit(run_search, salary.market_query(ctx))
        company_rows = pool.submit(run_search, company_query) if company_query else None
        levels = pool.submit(salary.fetch_levels, ctx, fetch, now_iso, notes)
        talent = pool.submit(salary.fetch_talent, ctx, fetch, now_iso, notes)
        profile = pool.submit(company.research_company, ctx, search=run_search, fetch=fetch, notes=notes)
        market_results = market_rows.result()
        company_results = company_rows.result() if company_rows else []
        direct = [source for source in (levels.result(), talent.result()) if source]
        profile_result = profile.result()

    sources: list[dict[str, str]] = []
    if published_source:
        sources.append(published_source)
    sources.extend(salary.company_sources_from_results(ctx, company_results, now_iso))
    sources.extend(direct)
    direct_sites = {salary.site_name(source["url"]) for source in direct}
    sources.extend(source for source in salary.market_sources_from_results(ctx, market_results, now_iso)
                   if salary.site_name(source["url"]) not in direct_sites)
    has_figures = (published and published.get("low") is not None) or any(
        source["kind"] in {"company", "market"} for source in sources)
    if not has_figures:
        estimate = salary.estimate_source(ctx, now_iso)
        if estimate:
            sources.append(estimate)
    salary_info = salary.decide(ctx, published, sources, quantile=quantile, round_to=round_to)

    raw_results = [*market_results, *company_results, *profile_result.raw_results]
    if _mentions_risk(ctx.employer, raw_results):
        # Raw rows are not cached; keep the verdict as a note so a cache hit warns too.
        notes.append(RISK_NOTE)
    result = ResearchResult(
        job_id=job_id, researched_at=now_iso, salary=salary_info, company=profile_result.company,
        sources=sources, notes=list(dict.fromkeys(notes)),
        raw_results=raw_results,
        summary_origin=profile_result.summary_origin,
    )
    # Always cache: the engine reads the ask from this row when it fills a
    # salary field. Incomplete lookups and estimate-only results expire sooner
    # in cache.load so a temporary provider failure can be retried.
    if conn is not None and job_id:
        try:
            cache.store(conn, result, context_sha256=context_sha256)
        except sqlite3.Error as exc:
            result.notes.append(f"Research cache not saved: {exc.__class__.__name__}.")
    return result


# --------------------------------------------------------------------------- formatting


def _names(sources: list[dict[str, Any]], kind: str, limit: int = 3) -> str:
    names = list(dict.fromkeys(str(source.get("name") or "") for source in sources if source.get("kind") == kind))
    return ", ".join(name for name in names[:limit] if name)


def format_salary_lines(result: ResearchResult | dict[str, Any]) -> list[str]:
    """HTML-escaped Published/Company/Market/Estimate/Ask lines, only those with data."""
    data = result.to_dict() if isinstance(result, ResearchResult) else result
    pay, sources = data.get("salary") or {}, data.get("sources") or []
    currency, period = pay.get("currency"), pay.get("period") or "year"
    lines: list[str] = []
    published = next((source for source in sources if source.get("kind") == "published"), None)
    if published and pay.get("published_text"):
        text = " ".join(str(pay["published_text"]).split())
        text = text if len(text) <= 80 else text[:79].rstrip() + "…"
        lines.append(f"Published: {html.escape(text)} ({html.escape(str(published.get('name')))})")
    for kind, label in (("company", "Company"), ("market", "Market")):
        cohorts = {}
        for source in sources:
            if source.get("kind") != kind:
                continue
            descriptors = salary.source_evidence(source)
            key = salary.evidence_cohort(descriptors)
            cohorts.setdefault(key, []).append(source)
        for key, cohort in cohorts.items():
            summary = salary.summarize_sources(cohort, kind=kind, currency=currency, period=period)
            if not summary:
                continue
            text = salary.format_range(summary["low"], summary["high"], summary["currency"], period)
            details = [", ".join(summary["names"][:3])]
            if key[0] == "company_wide":
                details.append("seniority unspecified")
            if key[2] == "total_comp":
                details.append("total compensation")
            elif key[2] == "base":
                details.append("base salary")
            if key[3]:
                details.append(f"{key[1]} data used as {key[3]} proxy")
            if key[5] in {"average", "median"}:
                details.append(f"reported {key[5]}")
            lines.append(f"{label}: {html.escape(text)} ({html.escape('; '.join(details))})")
    if pay.get("basis") == "estimate":
        summary = salary.summarize_sources(sources, kind="benchmark", currency=currency, period=period)
        if summary:
            text = salary.format_range(summary["low"], summary["high"], summary["currency"], period)
            lines.append(f"Estimate: {html.escape(text)} ({html.escape(_names(sources, 'benchmark', 1))}; "
                         "estimate, not employer data)")
    ask = pay.get("ask_amount")
    if ask:
        lines.append(f"Ask: {html.escape(_ask_text(pay))}")
    return lines


_TARGET_LOCATIONS = {"uae": "Dubai", "saudi": "Riyadh", "switzerland": "Zurich"}


def _ask_text(pay: dict[str, Any]) -> str:
    ask, basis = int(pay["ask_amount"]), str(pay.get("ask_basis") or pay.get("basis") or "none")
    currency, period = pay.get("currency"), pay.get("period") or "year"
    market = next((name for name, units in _MARKET_UNITS.items() if units == (currency, period)), None)
    target = _flow().salary_target({"location": _TARGET_LOCATIONS[market]}) if market in _TARGET_LOCATIONS else None
    if target and int(target["amount"]) == ask and basis != "published":
        why = "configured target"
    elif basis in {"none", "configured"}:
        why = "configured target"
    elif pay.get("low") != pay.get("high") and ask == ask_from_range(pay.get("low"), pay.get("high")):
        why = f"75th pct of {basis} range"
    elif pay.get("low") == pay.get("high"):
        why = f"from {basis} observation"
    else:
        why = f"from {basis} range"
    return f"{salary.format_amount(ask, currency, compact=False)}/{period} ({why})"
