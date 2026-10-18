"""Spain Dev Jobs board: synthetic pages modelled on the live markup, no network."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest import mock

import pytest
import requests

import jobhunter_availability as availability
import jobhunter_sources
import job_scoring
import scraper
from jobhunter_sources import spain_devjobs as sdj

ROOT = "https://spain-devjobs.com"
UUID = "01a0f4fb-0e74-7e2f-b2ae-4637a22b710b"
SLUG = "acme-cloud-senior-api-engineer-9ubi3qv"
TITLE = "Senior API Engineer – Payments"  # the en dash garbles unless the page is read as UTF-8
MADRID = f"{ROOT}/jobs?city=madrid&sort=newest"
VALENCIA = f"{ROOT}/jobs?city=valencia&sort=newest"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
BADGES = {"english": "No Spanish required", "remote": "Remote (Spain)", "visa": "Visa sponsorship",
          "relocation": "Relocation support", "overseas": "Apply from abroad"}


@pytest.fixture(autouse=True)
def default_config(monkeypatch):
    monkeypatch.setattr(scraper, "CONFIG", deepcopy(scraper.DEFAULT_CONFIG))


class Page:
    """What rate_limited_get returns for a page sent without a charset."""

    def __init__(self, html="", status=200):
        self.content = html.encode("utf-8")
        self.status_code = status
        self.encoding = "ISO-8859-1"  # requests' guess for text/html without a charset

    @property
    def text(self):
        return self.content.decode(self.encoding, errors="replace")


def serve(pages):
    """A fake get(): known URLs answer from ``pages``, anything else is a 404."""
    def get(url, **kwargs):
        get.calls.append(url)
        page = pages.get(url, Page(status=404))
        if isinstance(page, Exception):
            raise page
        return page
    get.calls = []
    return get


def card(slug=SLUG, title=TITLE, company="Acme Cloud", location="Madrid, Spain", posted="Yesterday", *,
         uuid=UUID, badges=("english",), salary=""):
    meta = [f'<span class="job-row__company">{company}</span>', f"<span>{location}</span>" if location else "",
            f'<span class="job-row__salary">{salary}</span>' if salary else "", f"<span>{posted}</span>"]
    tag = f'<span class="opened-tag" data-opened-tag="{uuid}" hidden>Opened</span>' if uuid else ""
    marks = "".join(f'<span class="badge badge--{kind}"><svg aria-hidden="true"></svg>{BADGES[kind]}</span>' for kind in badges)
    return (f'<li class="job-row"><a class="job-row__link" href="/jobs/{slug}" aria-describedby="results-{uuid}-meta">'
            f'<img alt="{company}" class="company-logo company-logo--card" src="/logos/x"><span class="job-row__main">'
            f'<span class="job-row__title">{title}</span><span class="job-row__meta">{"".join(meta)}</span>'
            f'<span class="job-row__badges">{tag}<span class="job-badges">{marks}</span></span></span></a></li>')


def listing(*cards, next_href=None):
    nav = (f'<nav aria-label="Pagination" class="pagination"><ul><li><a aria-current="page" href="/jobs">1</a></li>'
           f'<li><a class="pagination__step" href="{next_href}" rel="next">Next</a></li></ul></nav>' if next_href else "")
    similar = card("other-co-staff-engineer-zzzzzzz", "Staff Engineer", company="Other Co")
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Tech jobs in Spain</title></head><body>'
            f'<main><section class="jobs-page__results"><ul>{"".join(cards)}</ul>{nav}</section>'
            f'<section class="related-jobs"><ul>{similar}</ul></section></main></body></html>')


FACTS = (("Apply from", "Spain only"), ("Spanish", "Not required"), ("English", "Business level"),
         ("Visa sponsorship", "Not stated"), ("Relocation support", "Not stated"), ("Salary", "€70k–90k / year"))
DESCRIPTION = ("<p><strong>This is us</strong></p><p>Acme Cloud builds payment APIs for European merchants.</p>"
               "<ul><li><p>5+ years with Java and Spring Boot</p></li><li><p>Event-driven microservices on AWS</p></li></ul>")


def detail(*, title=TITLE, company="Acme Cloud", canonical_host="spain-devjobs.com", slug=SLUG, schema=True):
    canonical = f"https://{canonical_host}/jobs/{slug}"
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": title, "description": DESCRIPTION,
               "datePosted": "2026-09-21T09:09:42.000Z", "employmentType": "FULL_TIME", "url": canonical,
               "hiringOrganization": {"@type": "Organization", "name": company, "sameAs": "https://acme-cloud.example"},
               "identifier": {"@type": "PropertyValue", "value": "d3a3cd8f-8dbb-4bf3-a494-e9953fbe336f"}}
    encoded = json.dumps(posting).replace("<", "\\u003c")  # as the board escapes it
    ld = f'<script type="application/ld+json">{encoded}</script>' if schema else ""
    button = (f'<a class="btn btn--primary apply-btn apply-btn--lg" data-apply-job="{UUID}" data-umami-event="apply" '
              f'href="/go/{UUID}" rel="nofollow noopener" target="_blank"><span class="apply-btn__label"><span>Apply</span>'
              f'<span class="btn__sub">on {company} website</span></span><span class="visually-hidden">(opens in a new tab)</span></a>')
    facts = "".join(f'<div class="key-facts__cell"><dt>{name}</dt><dd>{value}</dd></div>' for name, value in FACTS)
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{title} at {company} — Madrid</title><link rel="canonical" href="{canonical}">'
            f'<meta property="og:url" content="{canonical}">{ld}</head><body><main><div class="container job-page">'
            '<nav class="breadcrumbs"><a href="/">Home</a></nav><header class="job-header" id="job-header">'
            f'<div class="job-header__top"><a class="job-header__company" href="/companies/acme-cloud">'
            f'<span aria-label="{company}" class="company-logo company-logo--initials" role="img"><span aria-hidden="true">AC</span></span>'
            f'<span>{company}</span></a><div class="job-header__apply">{button}<p class="opened-note" hidden></p></div></div>'
            f'<h1 class="job-header__title">{title}</h1><ul class="job-header__meta"><li>Madrid, Spain</li><li>Hybrid</li>'
            '<li>Full-time</li><li>Posted <time datetime="2026-09-20T08:00:00.000Z">20 September 2026</time></li></ul>'
            f'<div class="job-header__badges"><span class="job-badges"><span class="badge badge--english">No Spanish required</span></span></div>'
            f'<dl class="key-facts">{facts}</dl></header><div class="job-page__columns"><article class="job-page__main">'
            f'<div class="prose job-description">{DESCRIPTION}</div><div class="job-page__apply">{button}</div>'
            '<section class="chip-links"><h2 class="chip-links__title">Skills</h2><ul><li><a href="/jobs/tech/java">Java</a></li>'
            '<li><a href="/jobs/tech/aws">AWS</a></li></ul></section></article><aside class="job-sidebar">'
            f'<dl class="key-facts">{facts}</dl>{button}</aside></div><section class="related-jobs">'
            f'<ul>{card("other-co-staff-engineer-zzzzzzz", "Staff Engineer", company="Other Co")}</ul></section></div></main>'
            f'<div class="sticky-apply"><div class="container sticky-apply__inner">{button}</div></div></body></html>')


def run(get, location="Madrid, Spain", **config):
    return list(sdj.scrape(None, None, location, get=get, config={**scraper.CONFIG, **config}))


def days_old(job):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(job["date_posted"])) / timedelta(days=1)


def test_cards_carry_every_field_the_scraper_stores():
    page = listing(card(),
                   card("orbit-labs-engineering-team-lead-storage-fo7txzl", "Engineering Team Lead, Storage", "Orbit Labs",
                        posted="8 days ago", uuid="01a0dd64-ac70-7f86-b78c-fbd2afd2aac8",
                        badges=("english", "remote", "relocation", "overseas"), salary="€94k–113k / year"),
                   card("plain-co-backend-developer-ab12cd3", "Backend Developer", "Plain Co", uuid="", badges=()),
                   card("../../companies/acme-cloud", "Senior Engineer"))
    get = serve({MADRID: Page(page)})
    [jobs] = run(get)
    first, second, third = jobs
    assert {key: value for key, value in first.items() if key != "date_posted"} == {
        "id": "sdj-9ubi3qv", "title": TITLE, "company": "Acme Cloud", "location": "Madrid, Spain",
        "url": f"{ROOT}/jobs/{SLUG}", "source": "Spain Dev Jobs", "apply_url": f"{ROOT}/go/{UUID}",
        "language_requirement": "board: no Spanish required"}
    assert 0.99 < days_old(first) < 1.01
    assert second["id"] == "sdj-fo7txzl" and second["company"] == "Orbit Labs" and 7.99 < days_old(second) < 8.01
    assert second["apply_url"] == f"{ROOT}/go/01a0dd64-ac70-7f86-b78c-fbd2afd2aac8"
    assert second["board_facts"] == "Remote (Spain); Relocation support; Apply from abroad"
    assert second["salary_text"] == "€94k–113k / year"
    assert third["apply_url"] == "" and third["language_requirement"] == "" and "board_facts" not in third
    assert get.calls == [MADRID]


def test_pagination_follows_the_next_link_until_the_last_page():
    get = serve({MADRID: Page(listing(card(), next_href="/jobs?city=madrid&amp;sort=newest&amp;page=2")),
                 f"{MADRID}&page=2": Page(listing(card("acme-cloud-tech-lead-q1w2e3r")))})
    pages = run(get)
    assert [[job["id"] for job in page] for page in pages] == [["sdj-9ubi3qv"], ["sdj-q1w2e3r"]]
    assert get.calls == [MADRID, f"{MADRID}&page=2"]


@pytest.mark.parametrize("failure", [Page(status=500), Page(status=429), requests.ConnectionError("reset")])
def test_an_error_on_a_later_page_keeps_the_pages_already_read(failure):
    get = serve({MADRID: Page(listing(card(), next_href="/jobs?city=madrid&sort=newest&page=2")),
                 f"{MADRID}&page=2": failure})
    assert [len(page) for page in run(get)] == [1]
    assert len(get.calls) == 2


def test_a_page_wholly_older_than_the_freshness_window_is_the_last():
    old = listing(card(posted="3 weeks ago"), card("acme-cloud-architect-o1d2o3d", "Architect", posted="1 year ago"),
                  next_href="/jobs?city=madrid&sort=newest&page=2")
    get = serve({MADRID: Page(old)})
    [page] = run(get)
    assert len(page) == 2 and days_old(page[1]) > 364
    assert get.calls == [MADRID]


def test_a_page_that_crosses_the_freshness_window_still_turns():
    get = serve({MADRID: Page(listing(card(), card("acme-cloud-architect-o1d2o3d", "Architect", posted="2 weeks ago"),
                                      next_href="/jobs?city=madrid&sort=newest&page=2")),
                 f"{MADRID}&page=2": Page(listing(card("acme-cloud-lead-o4d5o6d", "Lead", posted="2 months ago"),
                                                  next_href="/jobs?city=madrid&sort=newest&page=3"))})
    assert [len(page) for page in run(get)] == [2, 1]
    assert len(get.calls) == 2


def test_a_page_without_readable_dates_is_the_last():
    undated = Page(listing(card(posted="Sep 21"), next_href="/jobs?city=madrid&sort=newest&page=2"))
    get = serve({MADRID: undated, f"{MADRID}&page=2": undated})
    [page] = run(get)
    assert page[0]["date_posted"] == "" and get.calls == [MADRID]


def test_a_search_keyword_is_ignored_for_the_city_listing():
    get = serve({MADRID: Page(listing(card()))})
    assert list(sdj.scrape(None, "software architect", "Madrid, Spain", get=get, config=scraper.CONFIG))[0][0]["id"] == "sdj-9ubi3qv"
    assert get.calls == [MADRID]


def test_an_empty_page_stops_and_similar_jobs_outside_the_results_never_count():
    get = serve({MADRID: Page(listing(next_href="/jobs?city=madrid&sort=newest&page=2"))})
    assert run(get) == []
    assert get.calls == [MADRID]


def test_max_pages_caps_the_listing_requests():
    always_next = Page(listing(card(), next_href="/jobs?city=madrid&sort=newest&page=2"))
    get = serve({MADRID: always_next, f"{MADRID}&page=2": always_next, f"{MADRID}&page=3": always_next})
    assert len(run(get, max_pages=2)) == 2
    assert len(get.calls) == 2


@pytest.mark.parametrize("href", ["https://evil.example/jobs?page=2", f"/go/{UUID}", "/api/jobs?page=2", "http://[bad"])
def test_next_links_leaving_the_listing_are_not_followed(href):
    get = serve({MADRID: Page(listing(card(), next_href=href))})
    assert len(run(get)) == 1
    assert get.calls == [MADRID]


def test_only_titles_worth_a_detail_fetch_are_kept():
    titles = ["Senior Backend Engineer", "Product Manager - Payment Infrastructure", "Account Executive",
              "Arquitecto de Software", "Staff Java Developer"]
    page = listing(*(card(f"acme-cloud-role-{index}abcdef", title) for index, title in enumerate(titles)))
    [jobs] = run(serve({MADRID: Page(page)}))
    assert [job["title"] for job in jobs] == ["Senior Backend Engineer", "Arquitecto de Software", "Staff Java Developer"]


def test_locations_become_the_listed_city_and_other_places_stay_rejectable():
    places = ["Valencia, Spain", "Remote (Spain)", "Málaga, Spain", "Hybrid", "Remote (EU)"]
    page = listing(*(card(f"acme-cloud-engineer-{index}abcdef", "Backend Engineer", location=place)
                     for index, place in enumerate(places)))
    [jobs] = run(serve({VALENCIA: Page(page)}), location="Valencia, Spain")
    locations = [job["location"] for job in jobs]
    assert locations == ["Valencia, Spain", "Valencia, Spain (remote)", "Málaga, Spain", "Valencia, Spain (hybrid)", "Remote (EU)"]
    assert [scraper.is_allowed_location(job) for job in jobs] == [True, True, False, True, False]


def test_barcelona_listing_keeps_chosen_cities_and_rejects_its_satellite_towns():
    places = ["Barcelona, Spain", "Madrid, Spain", "Sant Cugat del Vallès, Spain", "Remote (EU)"]
    page = listing(*(card(f"acme-cloud-engineer-{index}abcdef", "Backend Engineer", location=place)
                     for index, place in enumerate(places)))
    [jobs] = run(serve({f"{ROOT}/jobs?city=barcelona&sort=newest": Page(page)}), location="Barcelona, Spain")
    assert [job_scoring.location_allowed(job["location"], job_scoring.DEFAULT_MARKETS) for job in jobs] == [True, True, False, False]


def test_a_city_outside_the_chosen_markets_makes_no_request():
    get = serve({})
    assert run(get, location="Dubai") == [] and run(get, location="Sevilla, Spain") == []
    assert get.calls == []


@pytest.mark.parametrize("failure", [requests.Timeout("slow"), Page(status=500), Page("<html><body>maintenance</body></html>")])
def test_failures_yield_nothing_and_never_raise(failure):
    get = serve({MADRID: failure, f"{ROOT}/jobs/{SLUG}": failure})
    assert run(get) == []
    job = {"id": "sdj-9ubi3qv", "url": f"{ROOT}/jobs/{SLUG}", "title": TITLE, "company": "Acme Cloud"}
    assert sdj.fetch_description(None, job, get=get) == ""
    assert set(job) == {"id", "url", "title", "company"}


def test_fetch_description_returns_the_employer_text_and_enriches_the_job():
    job = {"id": "sdj-9ubi3qv", "url": f"{ROOT}/jobs/{SLUG}", "title": TITLE, "company": "Acme Cloud",
           "date_posted": "2026-09-20T12:00:00+00:00", "language_requirement": "board: no Spanish required"}
    get = serve({job["url"]: Page(detail())})
    text = sdj.fetch_description(None, job, get=get)
    assert text == ("This is us\n\nAcme Cloud builds payment APIs for European merchants.\n\n"
                    "5+ years with Java and Spring Boot\n\nEvent-driven microservices on AWS\n\nSkills: Java, AWS")
    assert job["date_posted"] == "2026-09-21T09:09:42+00:00"
    assert job["company_website"] == "https://acme-cloud.example"
    assert job["apply_url"] == f"{ROOT}/go/{UUID}"
    assert job["language_requirement"] == "board: Spanish not required; English business level"
    assert job["board_facts"] == "Apply from: Spain only; Visa sponsorship: Not stated; Relocation support: Not stated"
    assert job["salary_text"] == "€70k–90k / year"
    # Board commentary never reaches the text the sponsorship and salary readers see.
    assert not any(word in text for word in ("Visa", "Not stated", "Relocation", "€"))
    assert job_scoring.sponsorship_signal(text) == ("", "")
    assert get.calls == [job["url"]]


def test_a_malformed_employer_link_keeps_the_description():
    job = {"id": "sdj-9ubi3qv", "url": f"{ROOT}/jobs/{SLUG}", "title": TITLE, "company": "Acme Cloud"}
    page = detail().replace("https://acme-cloud.example", "http://[::1")
    assert sdj.fetch_description(None, job, get=serve({job["url"]: Page(page)})).startswith("This is us")
    assert "company_website" not in job


@pytest.mark.parametrize("url", [f"{ROOT}/go/{UUID}", f"{ROOT}/api/jobs", "https://evil.example/jobs/x-9ubi3qv", "http://[bad"])
def test_fetch_description_requests_only_board_job_pages(url):
    get = serve({})
    assert sdj.fetch_description(None, {"id": "sdj-9ubi3qv", "url": url}, get=get) == ""
    assert get.calls == []


def test_a_sister_canonical_page_without_json_ld_still_gives_text_and_date():
    job = {"id": "sdj-9ubi3qv", "url": f"{ROOT}/jobs/{SLUG}", "title": TITLE, "company": "Acme Cloud",
           "apply_url": f"{ROOT}/go/{UUID}", "salary_text": "€94k–113k / year"}
    text = sdj.fetch_description(None, job, get=serve({job["url"]: Page(detail(canonical_host="germanydevjobs.com", schema=False))}))
    assert text.startswith("This is us") and text.endswith("Skills: Java, AWS")
    assert job["date_posted"] == "2026-09-20T08:00:00+00:00"
    assert "company_website" not in job and job["salary_text"] == "€94k–113k / year"


def test_scraper_dispatches_description_fetches_to_the_board():
    job = {"id": "sdj-9ubi3qv", "url": f"{ROOT}/jobs/{SLUG}", "title": TITLE, "company": "Acme Cloud",
           "source": "Spain Dev Jobs"}
    with mock.patch.object(scraper, "rate_limited_get", return_value=Page(detail())) as get:
        assert scraper.fetch_job_description(mock.Mock(), job).endswith("Skills: Java, AWS")
    assert get.call_args.args[1] == job["url"]
    assert scraper.official_apply_url(job) == f"{ROOT}/go/{UUID}"


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.headers = body.encode(), status, {}

    def iter_content(self, chunk_size):
        yield from (self.body[index:index + chunk_size] for index in range(0, len(self.body), chunk_size))

    def close(self):
        pass


class Transport:
    def __init__(self, response):
        self.response, self.urls = response, []

    def send(self, request, **kwargs):
        self.urls.append(request.url)
        return self.response


JOB = {"id": "sdj-9ubi3qv", "source": "Spain Dev Jobs", "title": TITLE, "company": "Acme Cloud", "url": f"{ROOT}/jobs/{SLUG}"}


@pytest.mark.parametrize("page", [detail(), detail(canonical_host="germanydevjobs.com", schema=False)],
                         ids=["spain-canonical", "sister-canonical"])
def test_availability_opens_an_identified_live_job_page(page):
    transport = Transport(Response(page))
    result = availability.check(JOB, transport, now=NOW)
    assert (result["state"], result["reason"], result["matched"]) == ("open", "application_control", True)
    assert result["source_job_id"] == "9ubi3qv" and transport.urls == [JOB["url"]]


@pytest.mark.parametrize("page,reason", [(detail(title="Senior Frontend Engineer", schema=False), "listing_unverified"),
                                         (detail(company="Another Company", schema=False), "listing_unverified"),
                                         (detail(slug="acme-cloud-senior-api-engineer-zzzzzzz"), "identity_mismatch")],
                         ids=["other-title", "other-company", "other-job-canonical"])
def test_availability_stays_unknown_for_another_job(page, reason):
    result = availability.check(JOB, Transport(Response(page)), now=NOW)
    assert (result["state"], result["reason"]) == ("unknown", reason)


def test_ids_from_cards_are_accepted_by_the_availability_listing():
    [[job]] = run(serve({MADRID: Page(listing(card()))}))
    listing_ = availability._listing(job)
    assert listing_ is not None and listing_.source_id == "9ubi3qv" and listing_.url == JOB["url"]
    assert availability._listing({**job, "id": "sdj-other12"}) is None


def test_board_is_registered_before_linkedin_with_one_generator_per_city(monkeypatch):
    names = [name for name, _ in scraper.SCRAPERS]
    assert names.index("Spain Dev Jobs") < names.index("LinkedIn")
    board = jobhunter_sources.by_name("Spain Dev Jobs")
    assert board is sdj.BOARD and jobhunter_sources.by_job_id("sdj-9ubi3qv") is board
    buckets = scraper.build_collection_buckets(mock.Mock())
    expected = len(scraper.CONFIG["keywords"]) if board.keyword_search else 1
    for city in ("Valencia", "Madrid", "Barcelona"):
        assert len(buckets[f"Spain Dev Jobs/{city}"]["generators"]) == expected == 1
