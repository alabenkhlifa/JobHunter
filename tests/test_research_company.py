from pathlib import Path

import pytest

import jobhunter_interest_flow as flow
import jobhunter_research
from jobhunter_research import company, search

FIXTURES = Path(__file__).parent / "fixtures"


def cabify_results():
    return search.parse_brave_results((FIXTURES / "brave_company_cabify.html").read_text(encoding="utf-8"))


def job(**overrides):
    value = {
        "id": "sj-1",
        "title": "Senior Software Engineer",
        "company": "Cabify",
        "location": "Madrid, Spain",
        "description": "Build mobility services in Java.",
        "company_website": "",
        "source": "LinkedIn",
    }
    value.update(overrides)
    return value


def run(job_data, search_fn, *, pages=None, fetch=None):
    calls = {"search": [], "pages": []}

    def fake_search(query):
        calls["search"].append(query)
        return search_fn(query)

    def fake_pages(name, seeds, **kwargs):
        calls["pages"].append((name, seeds))
        return pages or []

    original = flow.fetch_verified_company_pages
    flow.fetch_verified_company_pages = fake_pages
    try:
        ctx = jobhunter_research.build_context(job_data)
        result = company.research_company(ctx, search=fake_search, fetch=fetch or (lambda url: None), notes=[])
    finally:
        flow.fetch_verified_company_pages = original
    return result, calls


def test_official_site_summary_and_facts_from_search_snippets():
    result, calls = run(job(), lambda query: cabify_results())

    profile = result.company
    assert calls["search"] == ['"Cabify" company official site about']
    assert calls["pages"] == []  # official results already describe the company
    assert profile["website"] == "https://cabify.com"
    assert profile["summary"].startswith("Cabify is a ride-sharing company that connects private users")
    assert "At Cabify, we are guided by our ambition" in profile["summary"]
    assert profile["summary"].count(".") <= 2
    assert profile["industry"] == "ride-sharing"
    assert profile["hq"] == "Madrid"
    assert profile["sources"][0] == "https://cabify.com"
    assert result.summary_origin == "web"


def test_job_company_website_comes_first():
    result, _ = run(job(company_website="https://www.cabify.es/careers"), lambda query: cabify_results())

    assert result.company["website"] == "https://www.cabify.es"


def test_useful_about_section_needs_no_web_lookup_when_site_is_known():
    description = (
        "About Revolut Our products include spending, saving, investing, and exchanging for 75+ million "
        "customers. We have 13,000+ people working around the world. About The Role We build our core platform."
    )
    result, calls = run(
        job(company="Revolut", company_website="https://www.revolut.com/", description=description),
        lambda query: pytest.fail("no search expected"),
    )

    assert calls == {"search": [], "pages": []}
    assert result.summary_origin == "job_post"
    assert result.company["summary"] == (
        "Revolut: Our products include spending, saving, investing, and exchanging for 75+ million customers. "
        "We have 13,000+ people working around the world."
    )
    assert result.company["sources"][0] == "job description"


def test_unknown_site_falls_back_to_verified_page_probe():
    page = {"title": "https://seedtag.es/", "url": "https://seedtag.es/",
            "snippet": "Seedtag es una empresa de publicidad contextual con sede en Madrid. Somos 500 empleados."}

    result, calls = run(job(company="Seedtag"), lambda query: [], pages=[page])

    assert calls["pages"] == [("Seedtag", [])]
    assert result.company["website"] == "https://seedtag.es"
    assert result.company["summary"].startswith("Seedtag es una empresa de publicidad contextual")
    assert result.company["size"] == "500 employees"


def test_probe_tries_spanish_domains():
    requested = []

    class Response:
        status_code = 404
        headers = {"content-type": "text/html"}
        text = ""

        def __init__(self, url):
            self.url = url

    def fetcher(url, **kwargs):
        requested.append(url)
        return Response(url)

    assert flow.fetch_verified_company_pages("Seedtag", [], fetcher=fetcher) == []
    assert "https://seedtag.es/" in requested


def test_facts_from_texts():
    facts = company.facts_from_texts([
        "Acme is a leading Spanish fintech platform headquartered in Barcelona, Spain. 1,001-5,000 employees.",
    ])

    assert facts == {"industry": "fintech", "size": "1,001-5,000 employees", "hq": "Barcelona, Spain"}


def test_summary_skips_junk_and_truncated_sentences():
    summary = company.summary_from_texts(
        "Cabify",
        ["Skip to main content · Help Center · Drivers", "Cabify is a mobility company in Spain and Latin America."],
        ["Cabify makes money by taking a commission from each ride booked through its platform ..."],
    )

    assert summary == "Cabify is a mobility company in Spain and Latin America."


def test_known_site_without_search_hits_uses_its_meta_description():
    fetched = []

    class Response:
        status_code = 200
        text = (
            '<html><head><meta name="description" content="Acme builds payment software for European banks.">'
            '<meta property="og:description" content="Acme builds payment software for European banks."></head></html>'
        )

    def fetch(url):
        fetched.append(url)
        return Response()

    result, calls = run(job(company="Acme Payments", company_website="https://acme.es/jobs"), lambda query: [],
                        fetch=fetch)

    assert calls["pages"] == []  # the site is known; no domain probing
    assert fetched == ["https://acme.es/"]
    assert result.company["summary"] == "Acme builds payment software for European banks."
    assert result.company["website"] == "https://acme.es"


def test_meta_descriptions_parser():
    html = """<meta content='Somos una consultora tecnol&oacute;gica' name='description'><meta name="viewport" content="x">"""

    assert company.meta_descriptions(html) == ["Somos una consultora tecnológica."]
