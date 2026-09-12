"""Synthetic source fixtures: no external network or authenticated sessions."""

from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
from unittest.mock import Mock

import pytest
import requests

import jobhunter_availability as availability

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
DESCRIPTION = "Build reliable backend services and collaborate with an experienced engineering team."
LINKEDIN = {"id": "li-12345", "source": "LinkedIn", "title": "Backend Engineer", "company": "Example Team",
            "url": "https://ae.linkedin.com/jobs/view/backend-engineer-example-team-12345?trackingId=unused"}
FOUNDIT = {**LINKEDIN, "id": "foundit-98765", "source": "Foundit",
           "url": "https://www.founditgulf.com/job/backend-engineer-example-team-98765"}
GULFTALENT = {**LINKEDIN, "id": "gulftalent-54321", "source": "GulfTalent",
              "url": "https://www.gulftalent.com/uae/jobs/backend-engineer_54321"}


class Response:
    def __init__(self, body="", status=200, headers=None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        yield from (self.body[index:index + chunk_size] for index in range(0, len(self.body), chunk_size))

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append((request, kwargs))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def page(*, source="linkedin", identifier=None, status="", apply=True, description=DESCRIPTION, extra="", schema=None):
    identifier = identifier or ("12345" if source == "linkedin" else "98765")
    if source == "linkedin":
        canonical = f"https://www.linkedin.com/jobs/view/{identifier}"
        header, company, desc = "top-card-layout", "topcard__org-name-link", "show-more-less-html__markup"
    else:
        canonical = f"https://www.founditgulf.com/job/backend-engineer-{identifier}"
        header, company, desc = "job-detail-header", "company-name", "job-description"
    return (f'<html><head><title>Backend Engineer</title><link rel="canonical" href="{canonical}">'
            + (f'<script type="application/ld+json">{json.dumps(schema)}</script>' if schema else "")
            + f'</head><body><section class="{header}"><h1>Backend Engineer</h1>'
            f'<a class="{company}">Example Team</a><span>{status}</span>'
            + ('<button>Apply now</button>' if apply else "")
            + f'{extra}</section><div class="{desc}">{description}</div></body></html>')


def schema(identifier="12345", **fields):
    return {"@type": "JobPosting", "title": "Backend Engineer", "hiringOrganization": {"name": "Example Team"},
            "url": f"https://www.linkedin.com/jobs/view/{identifier}", "description": DESCRIPTION, **fields}


def run(body, job=LINKEDIN):
    return availability.check(job, Transport(Response(body)), now=NOW)


@pytest.mark.parametrize("job,source", [(LINKEDIN, "linkedin"), (FOUNDIT, "foundit")])
def test_open_requires_identified_title_company_description_and_application_control(job, source):
    result = run(page(source=source), job)
    assert result["state"] == "open" and result["matched"] is True
    assert result["reason"] == "application_control"
    assert result["job_id"] == job["id"] and result["checked_at"] == "2026-09-09T12:00:00+00:00"
    assert "trackingId" not in result["url"]


@pytest.mark.parametrize("marker", ["No longer accepting applications", "This job posting is no longer available",
                                    "This job has expired", "This position has been filled"])
def test_explicit_main_listing_closed_markers_override_apply_control(marker):
    result = run(page(status=marker))
    assert result["state"] == "closed" and result["reason"] == "closed_marker"


def test_linkedin_current_closed_figure_markup_is_recognized():
    # Tag/class structure verified against an anonymous public listing. Only
    # synthetic job metadata is retained in this fixture.
    html = page(apply=False, extra='<figure class="closed-job closed-job__flavor topcard__flavor-row">'
                '<figcaption class="closed-job__flavor--closed">No longer\n accepting applications</figcaption></figure>')
    assert run(html)["state"] == "closed"


@pytest.mark.parametrize("status", [403, 429, 404, 410, 500, 204])
def test_http_errors_are_retryable_even_when_error_body_contains_closed_text(status):
    response = Response(page(status="No longer accepting applications"), status=status)
    transport = Transport(response)
    result = availability.check(LINKEDIN, transport, NOW)
    assert result["state"] == "unknown" and result["reason"] == "http_unavailable"
    assert response.closed and len(transport.requests) == 2


def test_timeout_reason_never_exposes_exception_headers_or_credentials():
    result = availability.check(LINKEDIN, Transport(requests.Timeout("secret cookie=private credential=token")), NOW)
    assert result["state"] == "unknown" and result["reason"] == "request_failed"
    assert "private" not in json.dumps(result) and "token" not in json.dumps(result)


@pytest.mark.parametrize("body", ["", "<h1>Something went wrong</h1>",
                                 "<title>Sign in | LinkedIn</title>No longer accepting applications",
                                 '<div class="authwall">Sign in</div>' + page(status="No longer accepting applications")])
def test_empty_and_login_pages_never_close_jobs(body):
    assert run(body)["state"] == "unknown"


@pytest.mark.parametrize("description", ["", "   ", "<p></p>", "<script>pretend description content should never prove this job exists</script>"])
def test_empty_description_alone_never_closes_or_opens_a_job(description):
    assert run(page(apply=False, description=description))["state"] == "unknown"
    assert run(page(description=description))["state"] == "unknown"


def test_exact_identified_closed_topcard_remains_authoritative_when_source_removes_description():
    result = run(page(status="No longer accepting applications", description=""))
    assert result["state"] == "closed" and result["matched"] is True
    assert run(page(identifier="99999", status="No longer accepting applications", description=""))["state"] == "unknown"
    assert run(page(description="", schema=schema(description="", validThrough="2025-01-01T00:00:00Z")))["state"] == "unknown"


@pytest.mark.parametrize("changed", [{"title": "Another Role"}, {"company": "Different Company"}])
def test_same_url_with_another_title_or_company_is_not_verified(changed):
    assert run(page(), {**LINKEDIN, **changed})["state"] == "unknown"


def test_different_canonical_listing_cannot_supply_status_or_schema_evidence():
    result = run(page(identifier="99999", status="No longer accepting applications",
                      schema=schema(validThrough="2025-01-01T00:00:00Z")))
    assert result["state"] == "unknown" and result["reason"] == "identity_mismatch"


def test_recommended_job_closed_markers_and_expiry_do_not_close_main_open_job():
    result = run(page(extra='<aside class="similar-jobs"><span>No longer accepting applications</span></aside>',
                      schema={"@graph": [schema("99999", validThrough="2025-01-01T00:00:00Z")]}))
    assert result["state"] == "open"


def test_recommended_application_control_does_not_open_main_job():
    result = run(page(apply=False, extra='<aside class="similar-jobs"><button>Apply now</button></aside>'))
    assert result["state"] == "unknown"


def test_description_quoting_closed_marker_does_not_close_main_job():
    html = page(extra='<div class="show-more-less-html__markup"><p>No longer accepting applications</p></div>')
    assert run(html)["state"] == "open"


@pytest.mark.parametrize("attributes", ['hidden', 'class="hidden"', 'aria-hidden="true"', 'style="display: none"'])
def test_invisible_closed_templates_do_not_close_visible_open_listing(attributes):
    assert run(page(extra=f'<div {attributes}><span>No longer accepting applications</span></div>'))["state"] == "open"


def test_matching_structured_expiry_closes_listing_but_future_expiry_does_not_prove_open():
    assert run(page(schema=schema(validThrough="2026-09-08T20:00:00Z")))["reason"] == "expired_valid_through"
    assert run(page(apply=False, schema=schema(validThrough="2027-01-01T00:00:00Z")))["state"] == "unknown"


@pytest.mark.parametrize("expiry", ["invalid", "2026-09-09", "2026-09-08T10:00:00", "2027-01-01T00:00:00Z"])
def test_ambiguous_dates_and_future_expiry_do_not_close_listings(expiry):
    assert run(page(schema=schema(validThrough=expiry)))["state"] == "open"


def test_structured_identifier_must_not_conflict_with_its_own_url():
    assert run(page(apply=False, schema=schema(identifier={"value": "99999"}, validThrough="2025-01-01T00:00:00Z")))["state"] == "unknown"


def test_linkedin_guest_fallback_requires_matching_topcard_id_and_metadata():
    guest = (f'<section class="top-card-layout" data-entity-urn="urn:li:jobPosting:12345">'
             '<h1>Backend Engineer</h1><a class="topcard__org-name-link">Example Team</a>'
             '<button>Apply</button></section>'
             f'<div class="show-more-less-html__markup">{DESCRIPTION}</div>')
    transport = Transport(Response("", status=403), Response(guest))
    result = availability.check(LINKEDIN, transport, NOW)
    assert result["state"] == "open"
    assert [request.url for request, _ in transport.requests] == [
        "https://www.linkedin.com/jobs/view/12345", "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/12345"]


@pytest.mark.parametrize("control", ['<button disabled>Apply</button>', '<a href="javascript:apply()">Apply</a>',
                                    '<a href="#">Apply</a>', '<div hidden><button>Apply</button></div>',
                                    '<button aria-disabled="true">Apply</button>'])
def test_disabled_or_non_actionable_controls_do_not_prove_open(control):
    assert run(page(apply=False, extra=control))["state"] == "unknown"


def detail(**fields):
    return json.dumps({"jobDetailResponse": {"id": 98765, "title": "Backend Engineer", "companyName": "Example Team",
                                            "description": DESCRIPTION, **fields}})


def test_foundit_detail_api_requires_exact_identity_and_explicit_application_status():
    transport = Transport(Response(""), Response(detail(isActive=True, applyUrl="https://employer.example/apply")))
    assert availability.check(FOUNDIT, transport, NOW)["state"] == "open"
    assert transport.requests[-1][0].url == "https://www.founditgulf.com/middleware/jobdetail/98765"
    assert run(detail(status="EXPIRED"), FOUNDIT)["state"] == "closed"
    assert run(detail(isActive=True), FOUNDIT)["state"] == "unknown"
    assert run(detail(id=11111, status="EXPIRED"), FOUNDIT)["state"] == "unknown"
    assert run(detail(status="EXPIRED", description=""), FOUNDIT)["state"] == "unknown"


@pytest.mark.parametrize("url", ["http://www.linkedin.com/jobs/view/12345", "https://127.0.0.1/jobs/view/12345",
                                "https://169.254.169.254/latest/meta-data", "https://[::1]/jobs/view/12345",
                                "https://www.linkedin.com.evil.example/jobs/view/12345", "https://evil.linkedin.com/jobs/view/12345",
                                "https://www.linkedin.com:444/jobs/view/12345", "https://owner:secret@www.linkedin.com/jobs/view/12345",
                                "https://www.linkedin.com/jobs/search", "https://www.founditgulf.com/job/role-98765",
                                "https://www.linkedin.com/jobs/view/54321", "https://www.linkedin.com/jobs/view/12345#fragment"])
def test_unsupported_private_or_mismatched_input_urls_never_make_requests(url):
    transport = Transport(Response(page()))
    assert availability.check({**LINKEDIN, "url": url}, transport, NOW)["state"] == "unknown"
    assert transport.requests == []


@pytest.mark.parametrize('source,identifier,url', [
    ('linkedin', '4462147346', 'https://ch.linkedin.com/jobs/view/lead-software-engineer-%E2%80%93-data-migration-80-100%25-at-abraxas-informatik-ag-4462147346'),
    ('linkedin', '4459593960', 'https://ch.linkedin.com/jobs/view/architecte-de-solution-et-d-int%C3%A9gration-at-act-digital-emea-alter-solutions-4459593960'),
    ('linkedin', '12345', 'https://www.linkedin.com/jobs/view/ing%C3%A9nieur-80-100%25-12345'),
    ('foundit', '98765', 'https://www.founditgulf.com/job/ing%c3%a9nieur-80-100%25-98765'),
])
def test_encoded_unicode_and_percent_slugs_keep_exact_listing_identity(source, identifier, url):
    job = {**(LINKEDIN if source == 'linkedin' else FOUNDIT), 'url': url,
           'id': ('li-' if source == 'linkedin' else 'foundit-') + identifier}
    # The source's canonical URL may retain the encoded slug as well.
    body = page(source=source, identifier=identifier)
    canonical = (f'https://www.linkedin.com/jobs/view/{identifier}' if source == 'linkedin'
                 else f'https://www.founditgulf.com/job/backend-engineer-{identifier}')
    body = body.replace(canonical, url)
    transport = Transport(Response(body))
    result = availability.check(job, transport, NOW)
    assert result['state'] == 'open' and result['matched'] is True
    assert result['source_job_id'] == identifier
    if source == 'linkedin':
        assert result['url'] == f'https://www.linkedin.com/jobs/view/{identifier}'
        assert transport.requests[0][0].url == result['url']
    else:
        assert transport.requests[0][0].url.lower() == url.lower()


@pytest.mark.parametrize('source', ['linkedin', 'foundit'])
@pytest.mark.parametrize('slug', ['role-%', 'role-%2', 'role-%GG', 'role-%C3%28', 'role-%FF',
                                'role-%2F-other', 'role-%5c-other', 'role-%3F-other', 'role-%23-other',
                                'role-%00-other', 'role-%0D%0A-other'])
def test_malformed_encoding_or_encoded_route_delimiters_never_make_requests(source, slug):
    job = LINKEDIN if source == 'linkedin' else FOUNDIT
    url = (f'https://www.linkedin.com/jobs/view/{slug}-12345' if source == 'linkedin'
           else f'https://www.founditgulf.com/job/{slug}-98765')
    transport = Transport(Response(page(source=source)))
    assert availability.check({**job, 'url': url}, transport, NOW)['state'] == 'unknown'
    assert transport.requests == []


def test_encoded_slug_does_not_relax_local_id_or_redirect_identity():
    other = 'https://www.linkedin.com/jobs/view/ing%C3%A9nieur-54321'
    transport = Transport(Response(page()))
    assert availability.check({**LINKEDIN, 'url': other}, transport, NOW)['state'] == 'unknown'
    assert transport.requests == []
    redirected = Transport(Response(status=302, headers={'Location': other}))
    assert availability.check(LINKEDIN, redirected, NOW)['state'] == 'unknown'
    assert all(request.url.endswith('/12345') for request, _ in redirected.requests)


def test_same_job_redirect_with_encoded_slug_is_checked_and_followed():
    url = 'https://www.linkedin.com/jobs/view/ing%C3%A9nieur-80-100%25-12345'
    transport = Transport(Response(status=302, headers={'Location': url}), Response(page()))
    assert availability.check(LINKEDIN, transport, NOW)['state'] == 'open'
    assert transport.requests[1][0].url == url


@pytest.mark.parametrize("target", ["http://127.0.0.1/secrets", "https://169.254.169.254/latest/meta-data",
                                   "https://example.com/", "https://www.linkedin.com/login",
                                   "https://www.linkedin.com/jobs/view/99999"])
def test_redirects_never_leave_fixed_source_and_job_identity(target):
    transport = Transport(Response(status=302, headers={"Location": target}))
    assert availability.check(LINKEDIN, transport, NOW)["state"] == "unknown"
    assert all("linkedin.com" in request.url and "12345" in request.url for request, _ in transport.requests)
    assert len(transport.requests) == 2


def test_redirects_to_same_listing_are_followed_with_a_fixed_bound():
    transport = Transport(Response(status=302, headers={"Location": "https://www.linkedin.com/jobs/view/role-12345"}), Response(page()))
    assert availability.check(LINKEDIN, transport, NOW)["state"] == "open"
    looping = Transport(Response(status=302, headers={"Location": "https://www.linkedin.com/jobs/view/role-12345"}))
    assert availability.check(LINKEDIN, looping, NOW)["reason"] == "request_limit"
    assert len(looping.requests) == availability.MAX_REQUESTS


@pytest.mark.parametrize("declared", [False, True])
def test_response_size_is_bounded_even_without_content_length(declared):
    response = Response(b"x" * (availability.MAX_RESPONSE_BYTES + 1), headers={"Content-Length": str(availability.MAX_RESPONSE_BYTES + 1)} if declared else {})
    result = availability.check(LINKEDIN, Transport(response), NOW)
    assert result["state"] == "unknown" and result["reason"] == "response_too_large" and response.closed


def test_supplied_session_credentials_cookies_headers_and_proxies_are_not_sent(monkeypatch):
    transport = requests.Session()
    transport.auth = ("owner", "secret")
    transport.headers.update({"Authorization": "Bearer secret", "Cookie": "private-session"})
    transport.cookies.set("session", "private")
    transport.proxies.update({"https": "http://private-proxy:8080"})
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy:8080")
    send = Mock(return_value=Response(page()))
    monkeypatch.setattr(transport, "send", send)
    try:
        assert availability.check(LINKEDIN, transport, NOW)["state"] == "open"
        request = send.call_args.args[0]
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        assert send.call_args.kwargs == {"timeout": availability.TIMEOUT, "allow_redirects": False, "stream": True, "proxies": {}, "verify": True, "cert": None}
    finally:
        transport.close()


def test_unknown_or_open_checks_preserve_job_state_and_application_history(tmp_path):
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as db, db:
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,url TEXT,status TEXT)")
        db.execute("INSERT INTO jobs VALUES(?,?,'interested')", (LINKEDIN["id"], LINKEDIN["url"]))
        db.execute("CREATE TABLE applications(job_id TEXT,stage TEXT)")
        db.execute("INSERT INTO applications VALUES(?,'package_generated')", (LINKEDIN["id"],))
        for result in (run(page(apply=False)), run(page())):
            availability.recordcheck(db, LINKEDIN, result)
            assert db.execute("SELECT status FROM jobs").fetchone()[0] == "interested"
        availability.recordcheck(db, LINKEDIN, run(page(status="No longer accepting applications")))
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "interested"
        assert db.execute("SELECT stage FROM applications").fetchone()[0] == "package_generated"
        assert db.execute("SELECT state,matched FROM job_availability").fetchall() == [("closed", 1)]


@pytest.mark.parametrize("status", ["new", "delivery_pending", "skipped", "rejected", "submitted", "interested"])
def test_only_pending_recommendations_become_unavailable(status):
    with closing(sqlite3.connect(":memory:")) as db, db:
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,url TEXT,status TEXT)")
        db.execute("INSERT INTO jobs VALUES(?,?,?)", (LINKEDIN["id"], LINKEDIN["url"], status))
        availability.recordcheck(db, LINKEDIN, run(page(status="No longer accepting applications")))
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == ("unavailable" if status in {"new", "delivery_pending"} else status)
        assert db.execute("SELECT state FROM job_availability").fetchone()[0] == "closed"


def test_persisting_check_rejects_wrong_job_url_and_fabricated_closed_evidence(tmp_path):
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as db, db:
        db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,url TEXT,status TEXT)")
        db.execute("INSERT INTO jobs VALUES(?,?,'new')", (LINKEDIN["id"], LINKEDIN["url"]))
        result = run(page())
        for changed in ({"job_id": "li-99999"}, {"url": "https://www.linkedin.com/jobs/view/99999"},
                        {"state": "closed"}, {"matched": False}, {"source_job_id": "99999"}):
            with pytest.raises(ValueError):
                availability.recordcheck(db, LINKEDIN, {**result, **changed})
        db.execute("UPDATE jobs SET url='https://www.linkedin.com/jobs/view/99999'")
        with pytest.raises(ValueError, match="changed"):
            availability.recordcheck(db, LINKEDIN, result)


def gulf_page(*, identifier="54321", description=DESCRIPTION, status="", application=True,
              path=None, widget_attrs="", extra=""):
    # Public mobile markup inspected locally; all listing data is synthetic.
    path = path if path is not None else (
        f"/register?journey=apply-mobile&amp;return=/apply/{identifier}&amp;job_id={identifier}&amp;is_external=0")
    widget = (f'<div class="react-job-application-button-mobile" path="{path}" {widget_attrs}></div>'
              if application else "")
    return (f'<title>Backend Engineer | GulfTalent</title><link rel="canonical" '
            f'href="https://www.gulftalent.com/uae/jobs/backend-engineer_{identifier}">'
            '<div class="header"><nav><a href="/register">Register</a></nav>'
            '<div class="container-fluid status"><div data-cy="mobile-header"><h1>Backend Engineer</h1></div></div>'
            '<div class="container-fluid subheader"><a class="mobile-company-link"><h2>Example Team</h2></a>'
            f'<p>{status}</p>{widget}</div></div><div id="content"><div class="job-description">{description}</div>'
            f'{extra}</div>')


def test_gulftalent_identified_listing_with_job_bound_application_entry_is_open():
    result = run(gulf_page(), GULFTALENT)
    assert (result["state"], result["reason"], result["matched"]) == (
        "open", "source_application_enabled", True)


@pytest.mark.parametrize("path", ["", "/register", "/apply/54321",
    "/register?journey=apply-mobile&amp;return=/apply/11111&amp;job_id=54321",
    "/register?journey=apply-mobile&amp;return=/apply/54321&amp;job_id=11111",
    "/register?journey=apply-mobile&amp;return=/apply/54321&amp;job_id=54321&amp;job_id=11111",
    "https://example.test/register?journey=apply-mobile&amp;return=/apply/54321&amp;job_id=54321",
])
def test_gulftalent_generic_or_mismatched_application_entries_remain_unknown(path):
    assert run(gulf_page(path=path), GULFTALENT)["state"] == "unknown"


@pytest.mark.parametrize("attrs", ['hidden', 'aria-hidden="true"', 'disabled',
                                   'aria-disabled="true"', 'style="display:none"'])
def test_gulftalent_hidden_and_disabled_application_entries_remain_unknown(attrs):
    assert run(gulf_page(widget_attrs=attrs), GULFTALENT)["state"] == "unknown"


def test_gulftalent_checks_require_matching_identity_company_and_description():
    for changes in ({"title": "Other Role"}, {"company": "Other Company"},
                    {"id": "gulftalent-11111"}, {"id": "foundit-54321"}):
        assert run(gulf_page(), {**GULFTALENT, **changes})["state"] == "unknown"
    assert run(gulf_page(identifier="11111"), GULFTALENT)["state"] == "unknown"
    assert run(gulf_page(description=""), GULFTALENT)["state"] == "unknown"
    assert run(gulf_page(status="This job has expired"), GULFTALENT)["state"] == "closed"


def test_gulftalent_recommendation_widget_does_not_prove_main_job_is_open():
    widget = '<div class="react-job-application-button-mobile" path="/register?journey=apply-mobile&amp;return=/apply/54321&amp;job_id=54321"></div>'
    assert run(gulf_page(application=False, extra=widget), GULFTALENT)["state"] == "unknown"


@pytest.mark.parametrize("separator", ["-", "_"])
@pytest.mark.parametrize("country", ["uae", "saudi-arabia", "qatar", "kuwait", "bahrain", "oman"])
def test_gulftalent_url_identity_accepts_gulf_countries_and_both_slug_formats(country, separator):
    assert availability._url_identity(f"https://www.gulftalent.com/{country}/jobs/backend{separator}54321") == (
        "gulftalent", "54321")


@pytest.mark.parametrize("status", [403, 429, 404, 500])
def test_gulftalent_http_failures_do_not_retry_through_another_endpoint(status):
    transport = Transport(Response(gulf_page(), status=status))
    result = availability.check(GULFTALENT, transport, NOW)
    assert result["state"] == "unknown" and result["reason"] == "http_unavailable"
    assert len(transport.requests) == 1


def test_gulftalent_redirect_to_login_is_not_followed():
    transport = Transport(Response(status=302, headers={"Location": "/candidates/login"}))
    assert availability.check(GULFTALENT, transport, NOW)["reason"] == "unsafe_redirect"
    assert len(transport.requests) == 1
