"""Company profile: official site, a short summary, industry/size/HQ when stated."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

PROFILE_HOSTS = ("wikipedia.org", "crunchbase.com", "linkedin.com", "craft.co", "cbinsights.com", "pitchbook.com",
                 "dealroom.co", "tracxn.com", "glassdoor.", "indeed.")
_JUNK = re.compile(r"cookie|javascript|skip to (?:main )?content|sign in|log in|iniciar sesi[oó]n|©", re.IGNORECASE)
_DATE_PREFIX = re.compile(
    r"^(?:[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}|\d+ (?:hours?|days?|weeks?|months?|years?) ago)\s*[-–—]\s*")


def _flow():
    import jobhunter_interest_flow as flow

    return flow


@dataclass
class CompanyResearch:
    company: dict[str, Any]
    raw_results: list[dict[str, str]] = field(default_factory=list)
    summary_origin: str = ""


def profile_query(name: str) -> str:
    return f'"{name}" company official site about'


def _root(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _clean(text: str) -> str:
    return _DATE_PREFIX.sub("", " ".join(str(text or "").split()))


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z¿¡\"“])", _clean(text))
    return [part.strip() for part in parts if 25 <= len(part.strip()) <= 260 and not _JUNK.search(part)
            and not part.strip().endswith(("...", "…"))]


def _alias_pattern(name: str) -> str:
    aliases = _flow().company_identity_aliases(name) or [name]
    return "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))


def summary_from_texts(name: str, official: list[str], others: list[str], *, curated: list[str] | None = None) -> str:
    """Up to two sentences: a definition ('X is a ...') then one from the official site.

    ``official``/``curated`` texts are the company's own words (meta
    descriptions, official-domain snippets); ``others`` (third-party snippets,
    raw page text) only contribute an explicit definition sentence.
    """
    alias = _alias_pattern(name)
    if not alias:
        return ""
    present = re.compile(rf"^(?:{alias})\b[^.]{{0,40}}?\b(?:is|es|are|son)\b", re.IGNORECASE)
    past = re.compile(rf"^(?:{alias})\b[^.]{{0,40}}?\b(?:was|fue)\b", re.IGNORECASE)
    loose = re.compile(rf"^(?:At\s+|En\s+)?(?:{alias})\b|\b(?:{alias})\b", re.IGNORECASE)
    curated_sentences = [sentence for text in curated or [] for sentence in _sentences(text)]
    official_sentences = [*curated_sentences, *(sentence for text in official for sentence in _sentences(text))]
    other_sentences = [sentence for text in others for sentence in _sentences(text)]
    candidates = [*official_sentences, *other_sentences]
    first = next((s for s in candidates if present.search(s)), "") or next((s for s in candidates if past.search(s)), "")
    if not first:
        first = next((s for s in official_sentences if loose.search(s)), "")
    if not first and curated_sentences:
        first = curated_sentences[0]
    if not first:
        return ""
    second = next(
        (s for s in official_sentences if s != first and (loose.search(s) or re.match(r"(?:We|Our|Somos|Nuestr)", s))),
        "",
    )
    summary = first if not second or len(first) + len(second) > 320 else f"{first} {second}"
    return summary[:320]


_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_NAME = re.compile(r"""(?:name|property)\s*=\s*["'](?:description|og:description|twitter:description)["']""", re.I)
_META_CONTENT = re.compile(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def meta_descriptions(html_text: str) -> list[str]:
    """The page's own description meta tags, deduplicated."""
    found = []
    for tag in _META_TAG.findall(html_text or ""):
        if _META_NAME.search(tag):
            content = _META_CONTENT.search(tag)
            value = " ".join(html.unescape((content.group(1) or content.group(2) or "") if content else "").split())
            if value:
                found.append(value if value.endswith((".", "!", "?")) else value + ".")
    return list(dict.fromkeys(found))


def _fetch_meta(fetch: Callable, website: str, notes: list[str]) -> list[str]:
    try:
        response = fetch(website.rstrip("/") + "/")
    except Exception as exc:  # noqa: BLE001 - best-effort
        notes.append(f"Company site fetch failed: {exc.__class__.__name__}.")
        return []
    if response is None or getattr(response, "status_code", 200) != 200:
        notes.append("Company site fetch unavailable.")
        return []
    return meta_descriptions(str(getattr(response, "text", "") or ""))


_INDUSTRY = re.compile(
    r"\b(?:is|es)\s+(?:a|an|un|una)\s+(?:(?:leading|global|spanish|european|international|innovative|fast-growing)\s+)*"
    r"((?:[\w-]+\s+){0,3}?)(?:company|platform|provider|consultancy|firm|startup|scale-?up|marketplace|empresa|bank)\b",
    re.IGNORECASE,
)
_INDUSTRY_LABEL = re.compile(r"\bIndustry[:\s]+([A-Z][\w &/,-]{2,40}?)(?:[.·|]|\s{2}|$)")
_SIZE = re.compile(
    r"\b(\d{1,3}(?:[,.]\d{3})*(?:\s*[-–]\s*\d{1,3}(?:[,.]\d{3})*)?\+?)\s+(?:employees|empleados|people|staff)\b",
    re.IGNORECASE,
)
_HQ = (
    re.compile(r"\bheadquartered in ([A-Z][\w'-]+(?:,\s*[A-Z][\w'-]+)?)"),
    re.compile(r"\b([A-Z][\w'-]+)-headquartered\b"),
    re.compile(r"\bHeadquarters?:?\s+([A-Z][\w'-]+(?:,\s*[A-Z][\w'-]+)?)"),
    re.compile(r"\bsede (?:central |principal )?en ([A-Z][\w'-]+)"),
    re.compile(r"\bis an? ([A-Z][\w'-]+)-based\b"),
)


def facts_from_texts(texts: list[str]) -> dict[str, str]:
    joined = [_clean(text) for text in texts if text]
    industry = size = hq = ""
    for text in joined:
        if not industry:
            label = _INDUSTRY_LABEL.search(text)
            match = _INDUSTRY.search(text)
            if label:
                industry = label.group(1).strip()
            elif match and match.group(1).strip():
                industry = match.group(1).strip()
        if not size:
            match = _SIZE.search(text)
            if match:
                size = f"{match.group(1)} employees"
        if not hq:
            for pattern in _HQ:
                match = pattern.search(text)
                if match:
                    hq = match.group(1).strip()
                    break
    return {"industry": industry, "size": size, "hq": hq}


def _official_results(name: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
    flow = _flow()
    return [result for result in results if flow._is_official_company_result(name, result.get("url", ""))]


def research_company(ctx, *, search: Callable[[str], list[dict[str, str]]], fetch: Callable,
                     notes: list[str], fetch_pages: bool = True) -> CompanyResearch:
    flow = _flow()
    name = ctx.employer
    search_name = flow.company_search_name(name) or name
    job_summary = flow._company_summary_from_job_description(name, ctx.description)
    useful_job_summary = flow._company_summary_has_useful_detail(name, job_summary)
    website = _root(ctx.company_website)

    raw: list[dict[str, str]] = []
    matched: list[dict[str, str]] = []
    if search_name and not (website and useful_job_summary):
        raw = search(profile_query(search_name)) or []
        matched = [result for result in raw if flow._result_matches_company(name, result)]
    official = _official_results(name, matched)
    if not website and official:
        website = _root(official[0]["url"])

    pages: list[dict[str, str]] = []
    meta: list[str] = []
    if fetch_pages and not useful_job_summary and not official:
        if not website:
            # Probe likely domains (.es/.com/...) and keep only pages whose
            # final host carries the company name.
            try:
                pages = flow.fetch_verified_company_pages(name, [], fetcher=_fetcher(fetch))
            except Exception as exc:  # noqa: BLE001 - company pages are best-effort
                notes.append(f"Company page fetch failed: {exc.__class__.__name__}.")
            if pages:
                website = _root(pages[0].get("url", ""))
        if website:
            meta = _fetch_meta(fetch, website, notes)

    official_texts = [result.get("snippet", "") for result in official]
    other_texts = [result.get("snippet", "") for result in matched if result not in official]
    page_texts = [page.get("snippet", "") for page in pages]
    origin = ""
    if useful_job_summary:
        summary, origin = job_summary, "job_post"
    else:
        summary = summary_from_texts(name, official_texts, [*other_texts, *page_texts], curated=meta)
        origin = "web" if summary else ""
        if not summary and (pages or matched):
            summary = flow._compact_company_summary(name, ctx.location, [*pages, *official, *matched][:3])
            origin = "web"
    facts = facts_from_texts([ctx.about_text, *meta, *official_texts, *other_texts, *page_texts])
    source_urls = [url for url in [website, *(page.get("url", "") for page in pages),
                                   *(result.get("url", "") for result in [*official, *matched])] if url]
    if useful_job_summary:
        source_urls.insert(0, "job description")
    company = {
        "name": name,
        "website": website,
        "summary": summary,
        "industry": facts["industry"],
        "size": facts["size"],
        "hq": facts["hq"],
        "sources": list(dict.fromkeys(source_urls))[:5],
    }
    return CompanyResearch(company=company, raw_results=raw, summary_origin=origin)


def _fetcher(fetch: Callable) -> Callable:
    """Adapt research ``fetch(url)`` to the requests-style call the page verifier makes."""

    def call(url: str, **_kwargs: Any):
        response = fetch(url)
        if response is None:
            raise OSError("fetch unavailable")
        return response

    return call
