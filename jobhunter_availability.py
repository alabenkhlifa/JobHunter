"""Anonymous, conservative live availability checks for supported job listings.

A successful HTTP response is not proof that applications are open. Only an
identified listing with a description and an explicit application control can
be delivered. Transport failures and ambiguous source pages remain retryable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as day_time, timedelta, timezone
import json
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUESTS = 4
MAX_REDIRECTS = 2
TOTAL_SECONDS = 20
TIMEOUT = (3.0, 6.0)
LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com", *(
    f"{country}.linkedin.com" for country in
    "ae sa uk de fr nl ch be es it ie sg ca us in au nz no se dk fi pt at pl cz lu ro gr tr za qa kw bh om eg ma tn".split()
)})
FOUNDIT_HOSTS = frozenset({"www.founditgulf.com", "founditgulf.com"})
GULFTALENT_HOSTS = frozenset({"www.gulftalent.com", "gulftalent.com"})
REASONS = frozenset({"unsupported_listing", "identity_mismatch", "request_failed", "http_unavailable",
                     "unsafe_redirect", "response_too_large", "request_limit", "login_or_challenge",
                     "missing_description", "listing_unverified", "no_open_evidence", "closed_marker",
                     "expired_valid_through", "application_control", "source_application_enabled"})
HEADERS = {"User-Agent": "JobHunter/1.0 (public listing availability)",
           "Accept": "text/html,application/json", "Cache-Control": "no-cache"}


@dataclass(frozen=True)
class Listing:
    source: str
    source_id: str
    url: str
    fallback_url: str


def _parsed_url(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment or not parsed.hostname):
            return None
        return parsed
    except ValueError:
        return None


def _url_identity(value):
    parsed = _parsed_url(value)
    if parsed is None:
        return None
    # Source slugs contain percent-encoded Unicode and punctuation. Validate
    # escapes before accepting them, and keep encoded path separators/control
    # characters from changing the route whose trailing job ID we recognize.
    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
        return None
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except UnicodeError:
        return None
    if (re.search(r"[\x00-\x1f\x7f\\?#]", decoded_path)
            or decoded_path.count("/") != parsed.path.count("/")):
        return None
    host = parsed.hostname.lower()
    if host in LINKEDIN_HOSTS:
        match = re.fullmatch(r"/jobs/view/(?:[^/?#]+-)?(\d{1,20})/?", parsed.path)
        guest = re.fullmatch(r"/jobs-guest/jobs/api/jobPosting/(\d{1,20})/?", parsed.path)
        if match or guest:
            return "linkedin", (match or guest).group(1)
    if host in FOUNDIT_HOSTS:
        match = re.fullmatch(r"/job/(?:[^/?#]+-)?(\d{1,20})(?:\.html)?/?", parsed.path)
        detail = re.fullmatch(r"/middleware/jobdetail/(\d{1,20})/?", parsed.path)
        if match or detail:
            return "foundit", (match or detail).group(1)
    if host in GULFTALENT_HOSTS:
        match = re.fullmatch(
            r"/(?:uae|saudi-arabia|qatar|kuwait|bahrain|oman)/jobs/[^/?#]+[-_](\d{1,20})/?", parsed.path)
        if match:
            return "gulftalent", match.group(1)
    return None


def _listing(job):
    identity = _url_identity(job.get("url"))
    if identity is None:
        return None
    source, source_id = identity
    declared = str(job.get("source") or "").strip().casefold()
    if declared and declared != source:
        return None
    local_id = str(job.get("id") or "")
    prefixes = {"linkedin": ("li-", "linkedin-"), "foundit": ("foundit-",), "gulftalent": ("gulftalent-",)}
    if any(local_id.startswith(values) for other, values in prefixes.items() if other != source):
        return None
    prefix = "(?:li|linkedin)" if source == "linkedin" else source
    local_match = re.fullmatch(prefix + r"-(\d+)", local_id)
    if local_match and local_match.group(1) != source_id:
        return None
    # Unrecognized local IDs may be database hashes. The public URL still has
    # to identify an exact supported source listing, then the page must match.
    if source == "linkedin":
        return Listing(source, source_id, f"https://www.linkedin.com/jobs/view/{source_id}",
                       f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{source_id}")
    parsed = _parsed_url(job["url"])
    if source == "gulftalent":
        url = f"https://www.gulftalent.com{parsed.path.rstrip('/')}"
        return Listing(source, source_id, url, url)
    url = f"https://www.founditgulf.com{parsed.path.rstrip('/')}"
    return Listing(source, source_id, url,
                   f"https://www.founditgulf.com/middleware/jobdetail/{source_id}")


class FetchFailure(Exception):
    pass


def _fetch(transport, url, listing, budget):
    redirects = 0
    while True:
        if budget[0] >= MAX_REQUESTS or time.monotonic() - budget[1] >= TOTAL_SECONDS:
            raise FetchFailure("request_limit")
        if _url_identity(url) != (listing.source, listing.source_id):
            raise FetchFailure("unsafe_redirect")
        budget[0] += 1
        # Preparing Request directly deliberately bypasses Session auth,
        # cookies, default headers, netrc and environment proxy configuration.
        prepared = requests.Request("GET", url, headers=HEADERS).prepare()
        response = None
        try:
            response = transport.send(prepared, timeout=TIMEOUT, allow_redirects=False, stream=True, proxies={}, verify=True, cert=None)
            if response.status_code in {301, 302, 303, 307, 308}:
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise FetchFailure("request_limit")
                location = response.headers.get("Location", "")
                if not location:
                    raise FetchFailure("unsafe_redirect")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise FetchFailure("http_unavailable")
            length = response.headers.get("Content-Length")
            if length and (not str(length).isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise FetchFailure("response_too_large")
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=8192):
                if time.monotonic() - budget[1] >= TOTAL_SECONDS:
                    raise FetchFailure("request_limit")
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise FetchFailure("response_too_large")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
        except requests.RequestException:
            raise FetchFailure("request_failed") from None
        finally:
            if response is not None:
                response.close()


def _text(value):
    return " ".join(BeautifulSoup(str(value or ""), "lxml").get_text(" ", strip=True).split())


def _same_text(first, second):
    return bool(_text(first)) and _text(first).casefold() == _text(second).casefold()


def _description(value):
    # Tiny placeholders, empty markup and script-only shells are not a listing.
    return len(_text(value)) >= 40


def _schema_nodes(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, list):
        for item in value[:100]:
            yield from _schema_nodes(item, depth + 1)
    elif isinstance(value, dict):
        kind = value.get("@type")
        if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
            yield value
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in value:
                yield from _schema_nodes(value[key], depth + 1)


def _schema_identity(node, listing):
    identities = []
    for key in ("url", "@id", "mainEntityOfPage"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("@id") or value.get("url")
        identity = _url_identity(value)
        if identity:
            identities.append(identity)
    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    if isinstance(identifier, (str, int)):
        match = re.fullmatch(r"(?:urn:li:jobPosting:)?(\d{1,20})", str(identifier))
        if match:
            identities.append((listing.source, match.group(1)))
    return bool(identities) and all(identity == (listing.source, listing.source_id) for identity in identities)


def _expired(value, now):
    if not isinstance(value, str):
        return False
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            # Date-only values have no timezone. Wait until that date has ended
            # even at UTC-12 before treating the listing as definitely expired.
            expiry = datetime.combine(datetime.fromisoformat(value).date(), day_time(), timezone.utc) + timedelta(days=1, hours=12)
        else:
            expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                return False
        return now > expiry
    except ValueError:
        return False


CLOSED = re.compile(r"^(?:(?:this|the) (?:job|job posting|position|vacancy) (?:is |has been |has )?)?"
                    r"(?:no longer accepting applications|no longer available|not accepting applications|"
                    r"closed|expired|removed|filled)[.!]?$", re.I)
APPLY = re.compile(r"^(?:apply|apply now|apply for this job|apply on company (?:site|website)|"
                   r"easy apply|quick apply)$", re.I)
HEADER_SELECTORS = {"linkedin": ".top-card-layout, .topcard, .jobs-unified-top-card, .job-details-jobs-unified-top-card",
                    "gulftalent": ".gulftalent-job-header",
                    "foundit": ".job-detail-header, .job-details-header, .jd-header, .job-header, [data-testid='job-header']"}
DESCRIPTION_SELECTORS = {"linkedin": ".show-more-less-html__markup, .description__text, .jobs-description-content__text",
                         "gulftalent": "#content .job-description",
                         "foundit": ".job-description, .job-details-description, .jobDescription, [itemprop='description']"}
COMPANY_SELECTORS = {"linkedin": ".topcard__org-name-link, .top-card-layout__first-subline a, .job-details-jobs-unified-top-card__company-name",
                     "gulftalent": ".mobile-company-link h2",
                     "foundit": ".company-name, .companyName, [itemprop='hiringOrganization']"}


def _visible(node):
    return not any(ancestor.has_attr("hidden") or ancestor.get("aria-hidden") == "true"
                   or "hidden" in ancestor.get("class", [])
                   or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", ancestor.get("style", ""), re.I)
                   for ancestor in (node, *node.parents) if getattr(ancestor, "attrs", None) is not None)


def _html_check(body, listing, job, now):
    soup = BeautifulSoup(body, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if (re.search(r"\b(?:sign in|log in|login|security verification|verify you are human|captcha)\b", title, re.I)
            or soup.select_one(".authwall, .challenge-page, #challenge-form, [data-testid='login-wall']")):
        return "unknown", "login_or_challenge", False
    schemas = []
    for script in soup.select("script[type='application/ld+json']")[:20]:
        try:
            schemas.extend(_schema_nodes(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError, RecursionError):
            continue
    schemas = [node for node in schemas if _schema_identity(node, listing) and _same_text(node.get("title"), job.get("title"))]
    if job.get("company") and str(job["company"]).casefold() != "unknown":
        schemas = [node for node in schemas if _same_text(
            node.get("hiringOrganization", {}).get("name") if isinstance(node.get("hiringOrganization"), dict) else "", job["company"])]
    # Recommendation widgets must never supply status, description or controls
    # for the main job. Matching structured data was collected before pruning.
    for node in soup.select("script, style, template, aside, [class*='similar-jobs'], [class*='related-jobs'], [class*='recommended-jobs'], .jobs-you-may-like, [data-testid='recommended-jobs']"):
        node.decompose()
    if listing.source == "gulftalent":
        # The public mobile page separates the title and company/application
        # entry point into sibling sections. Exclude site navigation and the
        # second application widget below the description/recommendations.
        status = soup.select_one(".header > .container-fluid.status")
        subheader = soup.select_one(".header > .container-fluid.subheader")
        if status is not None and subheader is not None and status.parent is subheader.parent:
            wrapper = soup.new_tag("section", attrs={"class": "gulftalent-job-header"})
            status.insert_before(wrapper)
            wrapper.append(status.extract())
            wrapper.append(subheader.extract())
    header = soup.select_one(HEADER_SELECTORS[listing.source])
    if header is None:
        header = soup.select_one("main > header, article > header")
    heading = header.select_one("h1, h2.topcard__title") if header is not None else None
    page_ids = []
    for link in soup.select("link[rel='canonical'], meta[property='og:url']"):
        identity = _url_identity(link.get("href") or link.get("content"))
        if identity:
            page_ids.append(identity)
    if header is not None:
        for node in (header, *header.select("[data-entity-urn], [data-job-id], a.topcard__link")):
            urn = re.fullmatch(r"urn:li:jobPosting:(\d{1,20})", str(node.get("data-entity-urn", "")))
            identifier = str(node.get("data-job-id", ""))
            identity = _url_identity(node.get("href"))
            if urn or re.fullmatch(r"\d{1,20}", identifier):
                page_ids.append((listing.source, urn.group(1) if urn else identifier))
            if identity:
                page_ids.append(identity)
    # A different canonical/top-card job makes the page untrustworthy even if
    # a recommendation's JSON-LD happens to refer to the requested job.
    if any(identity != (listing.source, listing.source_id) for identity in page_ids):
        return "unknown", "identity_mismatch", False
    header_match = bool(page_ids) and heading is not None and _same_text(heading.get_text(" ", strip=True), job.get("title"))
    if header_match and job.get("company") and str(job["company"]).casefold() != "unknown":
        company = header.select_one(COMPANY_SELECTORS[listing.source])
        header_match = company is not None and _same_text(company.get_text(" ", strip=True), job["company"])
    matched = bool(schemas) or header_match
    if not matched:
        return "unknown", "listing_unverified", False
    descriptions = [node.get("description") for node in schemas]
    if header_match:
        descriptions += [node.get_text(" ", strip=True) for node in soup.select(DESCRIPTION_SELECTORS[listing.source])]
        for node in header.select(DESCRIPTION_SELECTORS[listing.source]):
            node.decompose()
        # Some expired pages retain only the identified top card. An explicit
        # visible status on that card is sufficient; missing content alone is
        # never evidence of closure.
        if any(_visible(node) and CLOSED.fullmatch(" ".join(node.get_text(" ", strip=True).split()))
               for node in header.find_all(["span", "p", "div", "strong", "figure", "figcaption"])):
            return "closed", "closed_marker", True
    if not any(_description(value) for value in descriptions):
        return "unknown", "missing_description", True
    if any(_description(node.get("description")) and _expired(node.get("validThrough"), now) for node in schemas):
        return "closed", "expired_valid_through", True
    if header_match:
        if listing.source == "gulftalent" and _gulftalent_application_enabled(header, listing):
            return "open", "source_application_enabled", True
        for node in header.select("button, a"):
            if node.has_attr("disabled") or node.get("aria-disabled") == "true" or not _visible(node):
                continue
            label = " ".join((node.get_text(" ", strip=True) or node.get("aria-label") or "").split())
            href = node.get("href")
            if APPLY.fullmatch(label) and (node.name == "button" or href and not href.startswith("#") and _parsed_url(urljoin(listing.url, href))):
                return "open", "application_control", True
    return "unknown", "no_open_evidence", True


def _gulftalent_application_enabled(header, listing):
    """Recognize the source's job-bound React application entry configuration.

    This checks only the public listing. It never visits registration or apply.
    A generic register link, or another job's widget, is not open evidence.
    """
    for node in header.select(".react-job-application-button-mobile[path]"):
        if not _visible(node) or node.has_attr("disabled") or node.get("aria-disabled") == "true":
            continue
        path = str(node.get("path", "")).strip()
        parsed = _parsed_url(urljoin(listing.url, path))
        if not parsed or parsed.hostname not in GULFTALENT_HOSTS or parsed.path != "/register":
            continue
        query = parse_qs(parsed.query)
        if (query.get("job_id") == [listing.source_id]
                and query.get("return") == [f"/apply/{listing.source_id}"]
                and query.get("journey") == ["apply-mobile"]):
            return True
    return False


def _foundit_json(body, listing, job):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    detail = data.get("jobDetailResponse") if isinstance(data, dict) else None
    if not isinstance(detail, dict):
        return "unknown", "listing_unverified", False
    if (str(detail.get("id")) != listing.source_id or not _same_text(detail.get("title") or detail.get("cleanedJobTitle"), job.get("title"))
            or job.get("company") and str(job["company"]).casefold() != "unknown" and not _same_text(detail.get("companyName"), job["company"])):
        return "unknown", "identity_mismatch", False
    if not _description(detail.get("description")):
        return "unknown", "missing_description", True
    status = str(detail.get("status") or "").upper()
    if status in {"CLOSED", "EXPIRED", "REMOVED", "FILLED"} or detail.get("isActive") is False:
        return "closed", "closed_marker", True
    apply_url = _parsed_url(detail.get("applyUrl"))
    if detail.get("isApplyAllowed") is True or (apply_url and (detail.get("isActive") is True or status == "ACTIVE")):
        return "open", "source_application_enabled", True
    return "unknown", "no_open_evidence", True


def check(job, session=None, now=None):
    """Return safe structured evidence; unknown always means hold and retry.

    An injected session is a transport only: its auth/cookies/headers are never
    copied into requests. No browser or owner account is used.
    """
    job = dict(job)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Availability checks require a timezone-aware time.")
    now = now.astimezone(timezone.utc)
    listing = _listing(job)
    result = {"state": "unknown", "reason": "unsupported_listing", "checked_at": now.isoformat(timespec="seconds"),
              "job_id": str(job.get("id") or ""), "url": listing.url if listing else "",
              "source_job_id": listing.source_id if listing else "", "matched": False}
    if listing is None:
        return result
    transport = session if session is not None else requests.Session()
    budget = [0, time.monotonic()]
    try:
        for url in dict.fromkeys((listing.url, listing.fallback_url)):
            try:
                body = _fetch(transport, url, listing, budget)
                parsed = _foundit_json(body, listing, job) if listing.source == "foundit" else None
                state, reason, matched = parsed or _html_check(body, listing, job, now)
            except FetchFailure as exc:
                state, reason, matched = "unknown", str(exc), False
            except (ValueError, TypeError, RecursionError):
                state, reason, matched = "unknown", "listing_unverified", False
            result.update(state=state, reason=reason, matched=matched)
            if state != "unknown":
                break
    finally:
        if session is None:
            transport.close()
    return result


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS job_availability (
        job_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
        checked_at TEXT NOT NULL, url TEXT NOT NULL, source_job_id TEXT NOT NULL,
        matched INTEGER NOT NULL)""")


def recordcheck(conn, job, result):
    """Save the latest check in the caller's transaction; preserve applications."""
    job = dict(job)
    listing = _listing(job)
    if (result.get("job_id") != str(job.get("id") or "") or result.get("url") != (listing.url if listing else "")
            or result.get("source_job_id") != (listing.source_id if listing else "")
            or result.get("state") not in {"open", "closed", "unknown"} or result.get("reason") not in REASONS
            or type(result.get("matched")) is not bool
            or result.get("state") == "closed" and result.get("reason") not in {"closed_marker", "expired_valid_through"}
            or result.get("state") == "open" and result.get("reason") not in {"application_control", "source_application_enabled"}
            or result["state"] != "unknown" and (not result["matched"] or listing is None)):
        raise ValueError("Availability evidence does not belong to this job.")
    datetime.fromisoformat(result["checked_at"])
    row = conn.execute("SELECT url FROM jobs WHERE id=?", (job["id"],)).fetchone()
    if row is None or _url_identity(row[0]) != _url_identity(job.get("url")):
        raise ValueError("The collected job changed before its availability was recorded.")
    ensure_schema(conn)
    conn.execute("""INSERT INTO job_availability VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,
        checked_at=excluded.checked_at,url=excluded.url,source_job_id=excluded.source_job_id,matched=excluded.matched""",
        tuple(result[key] for key in ("job_id", "state", "reason", "checked_at", "url", "source_job_id", "matched")))
    if result["state"] == "closed":
        conn.execute("UPDATE jobs SET status='unavailable' WHERE id=? AND status IN ('new','delivery_pending')", (job["id"],))
