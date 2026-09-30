import json
import os
import sqlite3
from pathlib import Path

import pytest

import jobhunter_interest_flow as flow
import render_pdf as pdf_renderer


def sample_job(**overrides):
    job = {
        "id": "li-1",
        "title": "Full Stack Developer",
        "company": "AGAPI",
        "location": "Dubai, United Arab Emirates",
        "url": "https://example.com/job",
        "source": "LinkedIn",
        "score": 20,
        "description": "Build Java Spring Boot or Golang microservices with React and Kubernetes.",
        "tech_required": "java, spring boot, golang, microservices, kubernetes, aws",
        "tech_nice_to_have": "observability",
        "min_experience": 4,
        "salary": "",
        "work_model": "on-site",
        "recruiter_name": "Francisco Cabilatazan",
        "recruiter_company": "AGAPI",
        "recruiter_profile_url": "https://linkedin.example/recruiter",
        "company_website": "https://agapi.ae/",
        "credibility_notes": "",
    }
    job.update(overrides)
    return job


def test_salary_target_defaults_to_user_configured_30000(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", raising=False)

    assert flow.target_salary_aed_monthly() == 30000


def test_salary_target_can_be_configured(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "24000")

    assert flow.target_salary_aed_monthly() == 24000


def test_salary_market_resolves_from_job_location():
    assert flow.salary_market(sample_job(location="Dubai, United Arab Emirates")) == "uae"
    assert flow.salary_market(sample_job(location="Abu Dhabi")) == "uae"
    assert flow.salary_market(sample_job(location="Jeddah, Saudi Arabia")) == "saudi"
    assert flow.salary_market(sample_job(location="Zurich, Switzerland")) == "switzerland"


def test_salary_market_requires_a_location_on_an_actual_job():
    assert flow.salary_market(sample_job(location="")) is None
    assert flow.salary_market({}) is None
    assert flow.salary_market(None) == "uae"


def test_madrid_does_not_inherit_uae_salary_guidance():
    job = sample_job(location="Madrid, Spain", title="Backend Software Engineer")

    assert flow.salary_market(job) is None
    assert flow.salary_target(job) is None
    assert flow.estimate_salary_range(job) == "Salary not published; no configured target for this location."
    assert all("AED" not in query and "Dubai" not in query for query in flow.salary_search_queries(job["title"], job["location"]))


def test_target_salary_label_uses_the_market_currency_and_period():
    assert flow.target_salary_label(sample_job(location="Dubai")) == "AED 30k/month"
    assert flow.target_salary_label(sample_job(location="Jeddah")) == "SAR 30k/month"
    assert flow.target_salary_label(sample_job(location="Geneva")) == "CHF 130k/year"


def test_each_market_target_can_be_overridden_by_its_own_env_var(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_CHF_YEARLY", "150000")

    assert flow.target_salary_label(sample_job(location="Zurich")) == "CHF 150k/year"
    assert flow.target_salary_label(sample_job(location="Dubai")) == "AED 30k/month"


def test_estimated_band_never_quotes_the_wrong_currency(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_TARGET_SALARY_CHF_YEARLY", raising=False)
    swiss = flow.estimate_salary_range(
        sample_job(location="Zurich, Switzerland", title="Software Architect", min_experience=7)
    )

    assert "CHF" in swiss
    assert "AED" not in swiss


def test_uae_estimated_bands_are_unchanged(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", raising=False)
    band = flow.estimate_salary_range(
        sample_job(location="Dubai", title="Software Architect", min_experience=7)
    )

    assert band.startswith("AED 22k\u201330k/month")


def test_web_research_parser_extracts_result_and_unwraps_redirect():
    html = '''
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fagapi.ae%2F">AGAPI Information Technology</a>
    <a class="result__snippet">Custom software and data intelligence in Dubai.</a>
    '''

    results = flow.parse_duckduckgo_results(html)

    assert results == [
        {
            "title": "AGAPI Information Technology",
            "url": "https://agapi.ae/",
            "snippet": "Custom software and data intelligence in Dubai.",
        }
    ]


def test_web_search_prefers_firecrawl_when_configured(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:58427/")
    get_calls = []
    post_calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": [
                    {
                        "title": "Careers | TrueForge FZ-LLC",
                        "url": "https://trueforge.ae/career/",
                        "description": "Solutions Architect / Lead Consultant in Dubai.",
                    }
                ],
            }

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return Response()

    def fake_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        raise AssertionError("DuckDuckGo fallback should not be used when Firecrawl returns results")

    results = flow.web_search_results("TrueForge Dubai", timeout=4, fetcher=fake_get, poster=fake_post)

    assert results == [
        {
            "title": "Careers | TrueForge FZ-LLC",
            "url": "https://trueforge.ae/career/",
            "snippet": "Solutions Architect / Lead Consultant in Dubai.",
        }
    ]
    assert post_calls[0][0] == "http://127.0.0.1:58427/v1/search"
    assert not get_calls


def test_web_search_falls_back_to_duckduckgo_when_firecrawl_empty(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:58427")

    class FirecrawlResponse:
        status_code = 200

        def json(self):
            return {"success": True, "data": []}

    class DuckDuckGoResponse:
        status_code = 200
        text = '<a class="result__a" href="https://agapi.ae/">AGAPI</a><a class="result__snippet">Dubai software.</a>'

    results = flow.web_search_results(
        "AGAPI Dubai",
        timeout=4,
        poster=lambda *args, **kwargs: FirecrawlResponse(),
        fetcher=lambda *args, **kwargs: DuckDuckGoResponse(),
    )

    assert results[0]["url"] == "https://agapi.ae/"


def test_research_job_does_not_add_general_salary_results(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true")
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "salary" in query.lower():
            return [{"title": "Dubai Full Stack Developer salary", "url": "https://salary.example", "snippet": "Monthly salary AED 18k to AED 25k."}]
        return [{"title": "AGAPI complaint check", "url": "https://company.example", "snippet": "No scam report found, but verify contract."}]

    monkeypatch.setattr(flow, "web_search_results", fake_search)
    monkeypatch.setattr(flow, "fetch_verified_company_pages", lambda *args, **kwargs: [])

    research = flow.research_job(sample_job())

    assert calls
    assert research.company_summary == "AGAPI is a technology company."
    assert not research.company_salary_sources
    assert not research.salary_sources
    assert "No company-specific salary range found." in research.missing_signals
    assert any("scam/fraud/fake/complaint" in warning for warning in research.warnings)


def test_collect_salary_sources_dedupes_multiple_salary_sites(monkeypatch):
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "gulftalent" in query.lower():
            return [
                {"title": "Senior Solutions Architect Salaries in UAE | GulfTalent", "url": "https://www.gulftalent.com/uae/salaries/senior-solutions-architect", "snippet": "Average AED 29,500 per month, up to AED 45,000."},
                {"title": "Duplicate GulfTalent", "url": "https://www.gulftalent.com/uae/salaries/solution-architect", "snippet": "AED 25,000 per month."},
            ]
        if "payscale" in query.lower():
            return [{"title": "Solutions Architect Salary in UAE | PayScale", "url": "https://www.payscale.com/research/AE/Job=Solutions_Architect/Salary", "snippet": "Average annual salary AED 300,000."}]
        if "glassdoor" in query.lower():
            return [{"title": "Solutions Architect Salaries in Dubai | Glassdoor", "url": "https://www.glassdoor.com/Salaries/dubai-solutions-architect-salary.htm", "snippet": "Average salary AED 28,133."}]
        return []

    monkeypatch.setattr(flow, "web_search_results", fake_search)

    sources = flow.collect_salary_sources("Solutions Architect", "Dubai, UAE", max_sources=4)

    assert [s["source"] for s in sources] == ["GulfTalent", "PayScale", "Glassdoor"]
    assert len({s["url"].split('/')[2] for s in sources}) == 3
    assert len(calls) >= 3


def test_company_salary_search_rejects_snippet_only_false_positive(monkeypatch):
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "careers compensation" in query:
            return [
                {
                    "title": "Careers | TrueForge FZ-LLC",
                    "url": "https://trueforge.ae/career/",
                    "snippet": "Performance bonuses and profit sharing; no salary range published.",
                },
                {
                    "title": "Solutions Architect",
                    "url": "https://my.fa.ru/jobs/123",
                    "snippet": "Related result mentions TrueForge salary.",
                },
            ]
        if "glassdoor.com" in query:
            return [
                {
                    "title": "TrueForge Salaries",
                    "url": "https://www.glassdoor.com/Salary/TrueForge-Salaries.htm",
                    "snippet": "Solutions Architect AED 35k-45k/month.",
                }
            ]
        if "indeed.com" in query:
            return [
                {
                    "title": "Solutions Architect - S&P Global",
                    "url": "https://www.linkedin.com/jobs/view/4327227809/",
                    "snippet": "S&P never asks candidates to pay. Related: TrueForge Dubai.",
                }
            ]
        return []

    monkeypatch.setattr(flow, "web_search_results", fake_search)

    sources = flow.collect_company_salary_sources("TrueForge", "Solutions Architect", "Dubai")

    assert len(calls) == len(flow.company_salary_search_queries("TrueForge", "Solutions Architect", "Dubai"))
    assert [source["source"] for source in sources] == ["Company careers page", "Glassdoor"]
    assert all("S&P" not in source["title"] for source in sources)
    assert all("my.fa.ru" not in source["url"] for source in sources)


def test_company_aliases_and_role_family_are_generic():
    assert flow.company_search_name("Amazon Web Services (AWS)") == "Amazon"
    assert flow.company_search_name("Northstar Technology Services (NTS)") == "Northstar"
    assert flow.company_search_name("S&P Global LLC") == "S&P Global"
    assert flow.company_search_name("EY") == "EY"
    assert "NTS" in flow.company_identity_aliases("Northstar Technology Services (NTS)")
    assert flow._result_matches_company(
        "EY",
        {"title": "EY Salaries", "url": "https://www.glassdoor.com/Salary/EY-Salaries.htm"},
    )
    assert not flow._result_matches_company(
        "EY",
        {"title": "Sydney Salaries", "url": "https://example.com/sydney-salaries"},
    )
    assert flow.salary_role_title(
        "Security Assurance Solutions Architect, AWS Security Assurance Services"
    ) == "Solutions Architect"

    queries = flow.company_salary_search_queries(
        "Amazon Web Services (AWS)",
        "Security Assurance Solutions Architect, AWS Security Assurance Services",
        "Dubai, United Arab Emirates",
    )

    assert all('"Amazon"' in query for query in queries)
    assert all('"Solutions Architect"' in query for query in queries[1:])


def test_company_salary_search_does_not_assume_dubai_for_missing_location():
    queries = flow.company_salary_search_queries("Huspy", "Backend Engineer", "")

    assert queries
    assert all("Dubai" not in query and "UAE" not in query for query in queries)


def test_company_salary_search_accepts_alias_and_rejects_wrong_role(monkeypatch):
    def fake_search(query, **kwargs):
        if "levels.fyi" not in query:
            return []
        return [
            {
                "title": "Amazon Solution Architect Salary in Greater Dubai Area",
                "url": "https://www.levels.fyi/companies/amazon/salaries/solution-architect/locations/greater-dubai-area",
                "snippet": "Dubai total compensation ranges from AED 505K to AED 1.04M per year.",
            },
            {
                "title": "Amazon Software Engineer Salary in Greater Dubai Area",
                "url": "https://www.levels.fyi/companies/amazon/salaries/software-engineer/locations/greater-dubai-area",
                "snippet": "Dubai total compensation is AED 700K per year.",
            },
        ]

    monkeypatch.setattr(flow, "web_search_results", fake_search)

    sources = flow.collect_company_salary_sources(
        "Amazon Web Services (AWS)",
        "Security Assurance Solutions Architect, AWS Security Assurance Services",
        "Dubai, United Arab Emirates",
    )

    assert [source["source"] for source in sources] == ["Levels.fyi"]
    assert "solution-architect" in sources[0]["url"]


def test_levels_salary_fallback_validates_company_role_and_location(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:58427")
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": {
                    "markdown": (
                        "##### Amazon\n"
                        "Amazon Solution Architect Salaries in Greater Dubai Area\n"
                        "Solution Architect compensation in Greater Dubai Area at Amazon ranges from "
                        "AED 505K per year for L5 to AED 1.04M per year for L7. "
                        "The median yearly compensation package is AED 800K."
                    )
                },
            }

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    source = flow.fetch_levels_salary_source(
        "Amazon Web Services (AWS)",
        "Security Assurance Solutions Architect, AWS Security Assurance Services",
        "Dubai, United Arab Emirates",
        poster=fake_post,
    )

    assert source == {
        "source": "Levels.fyi",
        "title": "Amazon Solutions Architect salary in Dubai",
        "url": "https://www.levels.fyi/companies/amazon/salaries/solution-architect/locations/greater-dubai-area",
        "snippet": (
            "Solution Architect compensation in Greater Dubai Area at Amazon ranges from "
            "AED 505K per year for L5 to AED 1.04M per year for L7."
        ),
    }
    assert calls[0][0] == "http://127.0.0.1:58427/v1/scrape"


def test_levels_salary_fallback_tries_locale_variant_when_default_is_empty(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:58427")
    requested_urls = []

    class Response:
        status_code = 200

        def __init__(self, markdown):
            self.markdown = markdown

        def json(self):
            return {"success": True, "data": {"markdown": self.markdown}}

    def fake_post(url, **kwargs):
        requested_url = kwargs["json"]["url"]
        requested_urls.append(requested_url)
        if "/en-gb/" not in requested_url:
            return Response("")
        return Response(
            "##### ByteDance\n"
            "ByteDance Software Engineer Salaries in Greater Dubai Area\n"
            "Software Engineer compensation in Greater Dubai Area at ByteDance ranges from "
            "AED 409K per year to AED 481K per year."
        )

    source = flow.fetch_levels_salary_source(
        "ByteDance",
        "Backend Software Engineer, Office Intelligence",
        "Dubai, United Arab Emirates",
        poster=fake_post,
    )

    assert source is not None
    assert "/en-gb/companies/bytedance/" in source["url"]
    assert len(requested_urls) == 2


def test_validated_levels_source_is_added_even_when_search_found_glassdoor(monkeypatch):
    monkeypatch.setattr(
        flow,
        "web_search_results",
        lambda *args, **kwargs: [{
            "title": "ByteDance Software Engineer Salary in Dubai",
            "url": "https://www.glassdoor.com/Salary/ByteDance-Software-Engineer-Dubai.htm",
            "snippet": "Dubai average salary is $192,501 per year.",
        }],
    )
    monkeypatch.setattr(
        flow,
        "fetch_levels_salary_source",
        lambda *args, **kwargs: {
            "source": "Levels.fyi",
            "title": "ByteDance Software Engineer salary in Dubai",
            "url": "https://www.levels.fyi/companies/bytedance/salaries/software-engineer/locations/greater-dubai-area",
            "snippet": "Dubai total compensation ranges from AED 409K to AED 481K per year.",
        },
    )

    sources = flow.collect_company_salary_sources(
        "ByteDance",
        "Backend Software Engineer, Office Intelligence",
        "Dubai, United Arab Emirates",
    )

    assert sources[0]["source"] == "Levels.fyi"
    assert any(source["source"] == "Glassdoor" for source in sources)


def test_compact_company_summary_keeps_useful_verified_details():
    summary = flow._compact_company_summary(
        "TrueForge",
        "Dubai, UAE",
        [
            {
                "title": "TrueForge FZ-LLC",
                "snippet": "Independent technology consulting company focused on legacy modernization, systems integration and software architecture design.",
            },
            {
                "title": "TrueForge | LinkedIn",
                "snippet": "TrueForge is a Dubai-based technology consultancy. 2-10 employees.",
            },
        ],
    )

    assert summary == (
        "TrueForge is a Dubai-based technology consultancy focused on legacy-system modernization, "
        "systems integration, software architecture (2–10 employees)."
    )


def test_compact_company_summary_does_not_use_role_location_as_headquarters():
    summary = flow._compact_company_summary(
        "Google",
        "Dubai, UAE",
        [{"title": "About Google", "snippet": "Google is a global technology company working on cloud platforms and AI."}],
    )

    assert summary == "Google is a global technology company focused on cloud platforms, AI."
    assert "Dubai-based" not in summary


def test_company_summary_uses_only_late_about_section_not_role_requirements():
    description = (
        "The role modernizes legacy systems and owns software architecture and systems integration. "
        "Requirements include cloud experience. "
        "About MongoDB MongoDB provides a globally distributed database platform for the AI era. "
        "Its cloud-native platform serves 60,000 customers worldwide."
    )

    summary = flow._company_summary_from_job_description("MongoDB", description)

    assert summary == (
        "MongoDB is a global database technology company focused on cloud platforms, AI "
        "(60,000 customers)."
    )
    assert "legacy-system modernization" not in summary
    assert "systems integration" not in summary
    assert "software architecture" not in summary


def test_company_summary_uses_bounded_company_context_without_about_heading():
    description = (
        "Description The security team, part of Nimbus Cloud Services (NCS), provides scalable "
        "cloud services to enterprise customers migrating workloads to the cloud. "
        "Key job responsibilities Build compliance automation for one security team."
    )

    summary = flow._company_summary_from_job_description("Nimbus Cloud Services (NCS)", description)

    assert summary == "Nimbus Cloud Services (NCS) is a cloud technology company."
    assert "compliance" not in summary


def test_company_summary_recognizes_company_alias_in_job_context():
    description = (
        "Description The AWS Security Assurance Services team, a part of Amazon Web Services, "
        "provides scalable security solutions to enterprise customers as they migrate to the cloud. "
        "Key job responsibilities Build compliance automation for the team."
    )

    summary = flow._company_summary_from_job_description("Amazon Web Services (AWS)", description)

    assert summary == "Amazon Web Services (AWS) is a cloud technology company."


def test_research_resolves_aggregator_employer_and_keeps_posting_company(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "false")
    job = sample_job(
        company="TALENTMATE",
        description=(
            "Job Description About Revolut People deserve more from their money. "
            "Our products include spending, saving, investing, and exchanging for 75+ million customers. "
            "We have 13,000+ people working around the world. About The Role We build our core platform."
        ),
        company_website="",
        credibility_notes="posted by agency/aggregator",
    )

    research = flow.research_job(job)
    message = flow.build_research_brief_message(job, research)

    assert research.employer_name == "Revolut"
    assert research.posting_company == "TALENTMATE"
    assert "Revolut — Dubai, United Arab Emirates (via TALENTMATE)" in message
    assert "Job post: Revolut is a global financial technology company focused on digital financial services" in message
    assert "13,000+ employees; 75+ million customers" in message
    assert "no Revolut pay data" in message


def test_research_skips_redundant_company_lookup_when_job_about_section_is_useful(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true")
    monkeypatch.setattr(flow, "collect_company_salary_sources", lambda *args, **kwargs: [])

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("company lookup should use the available job-post fallback")

    monkeypatch.setattr(flow, "fetch_verified_company_pages", unexpected_lookup)
    monkeypatch.setattr(flow, "web_search_results", unexpected_lookup)
    job = sample_job(
        company="TALENTMATE",
        company_website="",
        description=(
            "About Revolut Our products include spending, saving, investing, and exchanging "
            "for 75+ million customers. We have 13,000+ people working around the world. "
            "About The Role We build our core platform."
        ),
        credibility_notes="posted by agency/aggregator",
    )

    research = flow.research_job(job)

    assert research.company_summary.startswith("Job post: Revolut is a global financial technology company")


def test_research_verifies_company_when_job_context_summary_is_generic(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true")
    monkeypatch.setattr(flow, "collect_company_salary_sources", lambda *args, **kwargs: [])
    calls = []

    def fake_pages(company, sources, **kwargs):
        calls.append(company)
        return [{
            "title": "ByteDance - Inspire Creativity, Enrich Life",
            "url": "https://www.bytedance.com/en/",
            "snippet": "ByteDance is a global technology company operating content and business platforms.",
        }]

    monkeypatch.setattr(flow, "fetch_verified_company_pages", fake_pages)
    job = sample_job(
        company="ByteDance",
        company_website="",
        description="Build ByteDance enterprise software products for internal staff services.",
    )

    research = flow.research_job(job)

    assert calls == ["ByteDance"]
    assert research.company_summary == "ByteDance is a global technology company."


def test_legacy_salary_for_another_country_is_not_displayed():
    job = sample_job(
        company="Google",
        location="Dubai, United Arab Emirates",
        salary="€88000 - €90500",
        description=(
            "Spain: €88000 - €90500 (EUR) + bonus + equity "
            "Netherlands: €114000 - €117000 (EUR) + bonus + equity"
        ),
    )
    research = flow.JobResearch(
        company_summary="Google is a global technology company.",
        legitimacy="No obvious warning.",
        employer_name="Google",
    )

    message = flow.build_research_brief_message(job, research)

    assert "€88000" not in message
    assert "No published range; no Google pay data" in message


def test_annual_flight_allowance_is_not_reported_as_published_salary():
    job = sample_job(
        company="Hantec Trader",
        salary="AED 2,500",
        description="Competitive compensation includes an annual flight allowance of AED 2,500.",
    )
    research = flow.JobResearch(company_summary="Hantec Trader careers page.", legitimacy="")

    assert flow.validated_job_salary(job) == ""
    assert "AED 2,500" not in flow.estimate_salary_range(job)
    message = flow.build_research_brief_message(job, research)
    assert "No published range" in message
    assert "AED 2,500" not in message


def test_stored_salary_without_posting_evidence_is_not_reported_as_published():
    job = sample_job(salary="AED 28,000", description="Build backend services.")
    assert flow.validated_job_salary(job) == ""
    assert "Published salary" not in flow.estimate_salary_range(job)


def test_salary_and_flight_allowance_in_one_posting_are_distinguished():
    description = "Base salary AED 28,000 per month; annual flight allowance AED 2,500."
    assert flow.validated_job_salary(sample_job(salary="AED 28,000", description=description)) == "AED 28,000"
    assert flow.validated_job_salary(sample_job(salary="AED 2,500", description=description)) == ""


def test_fetch_verified_company_pages_uses_only_discovered_official_domain():
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>TrueForge technology consulting company focused on legacy modernization and systems integration.</body></html>"

        def __init__(self, url):
            self.url = url

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response(url)

    pages = flow.fetch_verified_company_pages(
        "TrueForge",
        [{"url": "https://trueforge.ae/career/"}],
        fetcher=fake_get,
    )

    assert set(calls) == {
        "https://trueforge.ae/career/",
        "https://trueforge.ae/careers/",
        "https://trueforge.ae/about/",
        "https://trueforge.ae/",
    }
    assert len(pages) == 3
    assert all(page["url"].startswith("https://trueforge.ae/") for page in pages)


def test_fetch_verified_company_pages_probes_likely_domain_when_search_is_noisy():
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>TrueForge careers compensation open positions in Dubai.</body></html>"

        def __init__(self, url):
            self.url = url

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.startswith("https://trueforge.ae/"):
            return Response(url)
        raise OSError("not reachable")

    pages = flow.fetch_verified_company_pages("TrueForge", [], fetcher=fake_get)

    assert "https://trueforge.ae/career/" in calls
    assert pages
    assert all("trueforge.ae" in page["url"] for page in pages)


def test_fetch_verified_company_pages_does_not_expand_unverified_domains():
    calls = []

    class Response:
        status_code = 404
        headers = {"content-type": "text/html"}
        text = "Not found"

        def __init__(self, url):
            self.url = url

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response(url)

    pages = flow.fetch_verified_company_pages("Revolut", [], fetcher=fake_get)

    assert pages == []
    assert set(calls) == {
        "https://revolut.ae/",
        "https://revolut.com/",
        "https://revolut.io/",
        "https://revolut.ai/",
    }


def test_build_research_brief_is_concise_and_company_salary_first(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "30000")
    research = flow.JobResearch(
        company_summary="AGAPI appears to be a Dubai software/data/security consultancy.",
        legitimacy="Looks plausible; verify contract and compensation before investing time.",
        recruiter="Francisco Cabilatazan — public LinkedIn job poster.",
        salary_range="No company-specific range found. Market benchmark available.",
        sources=["https://agapi.ae/", "https://example.com/job"],
        warnings=["Salary not published."],
        missing_signals=["Published salary not found."],
        salary_sources=[{"source": "GulfTalent", "snippet": "Average AED 25k/month, up to AED 35k.", "url": "https://salary.example"}],
        company_salary_checks=flow.company_salary_check_labels("AGAPI"),
    )

    message = flow.build_research_brief_message(sample_job(), research)

    assert "Research" in message
    assert "AGAPI appears to be a Dubai software/data/security consultancy." in message
    assert "Pay:" in message
    assert "No published range; no AGAPI pay data" in message
    assert "Glassdoor, Indeed, PayScale, GulfTalent or Levels.fyi" in message
    assert "market" not in message.lower()
    assert "AED 25k" not in message
    assert "Base salary, currency, pay period and bonus/equity terms" in message
    assert len(message) < 600


def test_research_brief_surfaces_ode_role_bar_ai_check_and_posting_visa_evidence():
    job = sample_job(
        title="Staff Software Engineer (UAE)",
        company="Ode with Anthropic",
        description=(
            "Requirements: 8+ years of software engineering experience. "
            "You will deliver production applied AI systems with clients. "
            "Benefits include visa sponsorship."
        ),
        min_experience=8,
        score_breakdown="knocked out: wants 8+ years, over the 7 cap",
        sponsorship_signal="offered",
        sponsorship_evidence="Benefits include visa sponsorship.",
    )
    research = flow.JobResearch(company_summary="Job post describes a product engineering team.", legitimacy="")

    message = flow.build_research_brief_message(job, research)

    assert "Posting asks for 8+ years (above local 7-year search cap)" in message
    assert "Posting mentions production applied AI; confirm direct delivery evidence" in message
    assert "Posting says “Benefits include visa sponsorship.”" in message
    assert "confirm eligibility and terms" in message
    assert "candidate lacks" not in message.lower()


def test_research_brief_does_not_turn_stale_metadata_or_ai_assistance_into_job_requirements():
    job = sample_job(
        description="Build Java services. The company uses AI-assisted coding tools.",
        min_experience=8,
        score_breakdown="knocked out: wants 8+ years, over the 7 cap",
        sponsorship_signal="offered",
        sponsorship_evidence="Visa sponsorship",
    )

    message = flow.build_research_brief_message(job, flow.JobResearch(company_summary="Known company.", legitimacy=""))

    assert "<b>Fit checks:</b>" not in message
    assert "<b>Visa:</b>" not in message


def test_research_brief_does_not_call_relocation_visa_sponsorship():
    job = sample_job(description="We offer relocation support for this role.")

    message = flow.build_research_brief_message(job, flow.JobResearch(company_summary="Known company.", legitimacy=""))

    assert "visa sponsorship is unconfirmed" in message
    assert "Posting says" not in message


def test_research_brief_reports_explicit_sponsorship_exclusion_over_offer():
    job = sample_job(description="Benefits include visa sponsorship. No visa sponsorship for this role.")

    message = flow.build_research_brief_message(job, flow.JobResearch(company_summary="Known company.", legitimacy=""))

    assert "Posting rules out sponsorship" in message
    assert "Posting says" not in message


def test_research_brief_shows_company_salary_when_found():
    research = flow.JobResearch(
        company_summary="Official company page found.",
        legitimacy="No obvious warning.",
        sources=["https://agapi.ae/"],
        company_salary_sources=[
            {
                "source": "Glassdoor",
                "snippet": "AGAPI Solutions Architect in Dubai AED 35k–45k/month.",
                "url": "https://glassdoor.example/agapi",
            }
        ],
    )

    message = flow.build_research_brief_message(sample_job(), research)

    assert "Glassdoor: AGAPI Solutions Architect in Dubai AED 35k–45k/month." in message
    assert "No AGAPI pay data" not in message


def test_research_brief_prefers_one_validated_salary_source():
    research = flow.JobResearch(
        company_summary="ByteDance is a global technology company.",
        legitimacy="No obvious warning.",
        employer_name="ByteDance",
        company_salary_sources=[
            {
                "source": "Glassdoor",
                "title": "ByteDance Software Engineer Salary in Dubai",
                "snippet": "Dubai average salary is $192,501 per year.",
                "url": "https://glassdoor.example/bytedance",
            },
            {
                "source": "Levels.fyi",
                "title": "ByteDance Software Engineer salary in Dubai",
                "snippet": "Dubai total compensation ranges from AED 409K to AED 481K per year.",
                "url": "https://levels.example/bytedance",
            },
        ],
    )
    job = sample_job(
        company="ByteDance",
        title="Backend Software Engineer, Office Intelligence",
        salary="",
    )

    message = flow.build_research_brief_message(job, research)

    assert "Levels.fyi: Dubai total compensation ranges from AED 409K to AED 481K per year." in message
    assert "$192,501" not in message


def test_company_salary_for_another_location_is_not_displayed():
    research = flow.JobResearch(
        company_summary="Google is a global technology company.",
        legitimacy="No obvious warning.",
        employer_name="Google",
        company_salary_sources=[
            {
                "source": "Glassdoor",
                "title": "Google Partner Solution Architect Salary in Spain",
                "snippet": "Spain total pay €88,000–€90,500 per year.",
                "url": "https://glassdoor.example/google-spain",
            }
        ],
    )
    job = sample_job(company="Google", location="Dubai, United Arab Emirates", salary="")

    message = flow.build_research_brief_message(job, research)

    assert "€88,000" not in message
    assert "No published range; no Google pay data" in message


def test_salary_page_title_does_not_turn_allowance_into_pay():
    research = flow.JobResearch(
        company_summary="Hantec Trader careers page.",
        legitimacy="",
        company_salary_sources=[{
            "source": "Glassdoor",
            "title": "Hantec Trader Java Backend Developer salary in Dubai",
            "snippet": "Benefits include an AED 2,500 annual flight allowance.",
            "url": "https://glassdoor.example/hantec",
        }],
    )
    message = flow.build_research_brief_message(sample_job(company="Hantec Trader"), research)

    assert "No published range" in message
    assert "AED 2,500" not in message


def test_madrid_research_rejects_aed_default_locale_pay():
    research = flow.JobResearch(
        company_summary="Huspy careers page.",
        legitimacy="",
        company_salary_sources=[{
            "source": "Levels.fyi",
            "title": "Huspy Software Engineer salary in Madrid",
            "snippet": "Madrid total compensation AED 250K per year.",
            "url": "https://levels.example/huspy-madrid",
        }],
    )
    message = flow.build_research_brief_message(
        sample_job(company="Huspy", location="Madrid, Spain"), research
    )

    assert "No published range" in message
    assert "AED 250K" not in message


def test_madrid_research_accepts_local_euro_pay_evidence():
    research = flow.JobResearch(
        company_summary="Huspy careers page.",
        legitimacy="",
        company_salary_sources=[{
            "source": "Glassdoor",
            "title": "Huspy Software Engineer salary in Madrid",
            "snippet": "Madrid average base salary €65,000 per year.",
            "url": "https://glassdoor.example/huspy",
        }],
    )
    message = flow.build_research_brief_message(
        sample_job(company="Huspy", location="Madrid, Spain"), research
    )

    assert "Glassdoor: Madrid average base salary €65,000 per year." in message


def test_official_compensation_note_is_shown_as_benefits_not_salary():
    research = flow.JobResearch(
        company_summary="TrueForge is a Dubai-based technology consultancy.",
        legitimacy="No obvious warning.",
        company_salary_sources=[
            {
                "source": "Company careers page",
                "title": "Careers | TrueForge",
                "snippet": "Compensation includes bonuses and equity. A client saved AED 340k/year.",
                "url": "https://trueforge.ae/career/",
            }
        ],
    )

    message = flow.build_research_brief_message(sample_job(company="TrueForge"), research)

    assert "No published range; no TrueForge pay data" in message
    assert "Benefits:</b> bonus, equity mentioned; no figures." in message
    assert "AED 340k" not in message


def test_low_confidence_research_is_actionable_not_generic(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "30000")
    job = sample_job(
        company="TrueForge",
        title="Solutions Architect / Lead Consultant",
        company_website="",
        recruiter_name="",
        recruiter_profile_url="",
        salary="",
    )

    research = flow.build_default_research(job)
    message = flow.build_research_brief_message(job, research)

    assert research.confidence == "Low"
    assert "quick public web/company-page check" not in message
    assert "No independent company evidence" in message
    assert "No published range; no TrueForge pay data" in message
    assert "Base salary, currency, pay period and bonus/equity terms" in message


def test_research_brief_keyboard_has_apply_ignore_and_details():
    keyboard = flow.research_brief_keyboard("li-1", "https://example.com/job")

    rows = keyboard["inline_keyboard"]
    flattened = [button for row in rows for button in row]
    assert {button["text"] for button in flattened} >= {"✅ Apply", "🚫 Ignore", "📄 Details"}
    assert {button.get("callback_data") for button in flattened if "callback_data" in button} >= {"apply:li-1", "ignore:li-1"}
    assert any(button.get("url") == "https://example.com/job" for button in flattened)


def test_render_pdf_uses_project_python_and_reports_errors(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 1
        stderr = "Traceback\nmissing dep"
        stdout = ""

    monkeypatch.setattr(flow, "_project_python", lambda: "/repo/.venv/bin/python")
    monkeypatch.setattr(flow.subprocess, "run", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or Result())

    with pytest.raises(RuntimeError, match="missing dep"):
        flow._render_pdf("resume", tmp_path / "resume.json", tmp_path / "resume.pdf")

    assert calls[0][0][0] == "/repo/.venv/bin/python"
    assert calls[0][0][1] == "render_pdf.py"


def test_render_pdf_returns_structured_renderer_page_count(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stderr = ""
        stdout = 'renderer log\n{"jobhunter_pdf_render": 1, "mode": "resume", "pages": 1}\n'

    monkeypatch.setattr(flow.subprocess, "run", lambda *args, **kwargs: Result())

    page_count = flow._render_pdf("resume", tmp_path / "resume.json", tmp_path / "resume.pdf")

    assert page_count == 1


@pytest.mark.parametrize(
    "metadata",
    [
        "Resume PDF written without structured metadata",
        '{"jobhunter_pdf_render": 1, "mode": "cover", "pages": 1}',
        '{"jobhunter_pdf_render": 1, "mode": "resume", "pages": 0}',
        '{"jobhunter_pdf_render": 1, "mode": "resume", "pages": true}',
        "{malformed-json",
    ],
)
def test_render_pdf_rejects_invalid_page_metadata(monkeypatch, tmp_path, metadata):
    class Result:
        returncode = 0
        stderr = ""
        stdout = metadata

    monkeypatch.setattr(flow.subprocess, "run", lambda *args, **kwargs: Result())

    with pytest.raises(RuntimeError, match="did not report a valid page count"):
        flow._render_pdf("resume", tmp_path / "resume.json", tmp_path / "resume.pdf")


def test_prepare_application_package_defaults_to_resume_and_can_include_requested_cover(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    output_dir = tmp_path / "output"
    profile = tmp_path / "master-profile.json"
    profile.write_text(
        '{"name":"Ala Ben Khalifa","email":"jobs@example.com","headline":"Software Architect",'
        '"summary":"7+ years backend/cloud experience.","skills":{"Backend":["Java","Spring Boot","Go"]},'
        '"experience":[],"education":[]}',
        encoding="utf-8",
    )
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT, source TEXT, description TEXT, tech_required TEXT)")
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("li-1", "Full Stack Developer", "AGAPI", "Dubai", "https://example.com/job", "LinkedIn", "Build backend services", "java, spring boot"),
    )
    conn.commit(); conn.close()

    def fail_tracker_sync():
        raise RuntimeError("optional tracker unavailable")

    monkeypatch.setattr(flow.scraper, "sync_application_tracker_if_enabled", fail_tracker_sync)

    resume_only = flow.prepare_application_package(
        "li-1",
        db_path=db,
        profile_path=profile,
        output_dir=output_dir,
        render_pdfs=False,
    )

    assert resume_only.resume_pdf.exists()
    assert resume_only.cover_json is None
    assert resume_only.cover_pdf is None
    assert not (resume_only.package_dir / "CoverLetter.pdf").exists()
    assert json.loads(resume_only.manifest_json.read_text(encoding="utf-8"))["cover_letter_included"] is False

    package = flow.prepare_application_package(
        "li-1",
        db_path=db,
        profile_path=profile,
        output_dir=output_dir,
        render_pdfs=False,
        include_cover_letter=True,
    )

    assert package.package_dir.exists()
    assert package.resume_json.exists()
    assert package.cover_json.exists()
    assert package.manifest_json.exists()
    assert package.resume_pdf.name == "Resume.pdf"
    assert package.cover_pdf.name == "CoverLetter.pdf"
    assert oct(package.package_dir.stat().st_mode & 0o777) == "0o700"
    for private_file in (
        package.resume_json,
        package.cover_json,
        package.manifest_json,
        package.resume_pdf,
        package.cover_pdf,
    ):
        assert oct(private_file.stat().st_mode & 0o777) == "0o600"
    manifest = json.loads(package.manifest_json.read_text(encoding="utf-8"))
    assert manifest["tailoring_mode"] == "legacy_fallback"
    assert manifest["selected_variant_id"] is None
    assert manifest["cover_letter_included"] is True
    assert len(manifest["profile_sha256"]) == 64
    assert manifest["quality_checks"] == {
        "timeline_consistent": True,
        "role_variant_required": False,
        "role_variant_requirement_satisfied": True,
    }
    resume_payload = json.loads(package.resume_json.read_text(encoding="utf-8"))
    resume_text = json.dumps(resume_payload, ensure_ascii=False).lower()
    assert "tailored" not in resume_text
    assert "generated" not in resume_text
    assert "aligned with full stack developer" not in resume_text
    assert "agapi" not in resume_payload["headline"].lower()
    assert "full stack developer" not in resume_payload["headline"].lower()
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT stage, package_path, notes FROM applications WHERE job_id='li-1'").fetchone()
    conn.close()
    assert row[0] == "package_generated"
    assert str(package.package_dir) == row[1]
    assert "Resume and cover letter generated" in row[2]

    with sqlite3.connect(db) as submitted:
        flow.scraper.record_application_stage(submitted, "li-1", "submitted", sync=False)
    with pytest.raises(flow.TailoringReadinessError, match="already reached submission"):
        flow.prepare_application_package(
            "li-1", db_path=db, profile_path=profile, output_dir=output_dir,
            render_pdfs=False, include_cover_letter=True,
        )
    with sqlite3.connect(db) as submitted:
        assert submitted.execute("SELECT stage FROM applications WHERE job_id='li-1'").fetchone()[0] == "submitted"


def test_reviewed_agent_draft_replaces_template_body_in_optional_cover_package(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    profile_path = tmp_path / "master-profile.json"
    profile = {
        "name": "Candidate", "email": "candidate@example.com",
        "experience": [{"title": "Engineer", "company": "Example", "dates": "2021 - Present",
                        "bullets": [
                            "Resolved duplicate alerts caused by event retries through durable processing.",
                            "Led four engineers delivering Java services and production releases.",
                        ]}],
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    job = {"id": "job-one", "title": "Java Technical Lead", "company": "TargetCo",
           "description": "Lead Java services and investigate production integration failures."}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, description TEXT)")
        conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)", tuple(job.values()))
    context = flow.cover_letter_draft_context(profile, job)
    draft = {
        "paragraphs": [
            "Your technical lead role asks for someone who can stay close to backend delivery while a team handles difficult integrations. That combination matches the work I have done on production Java services, where reliability had to be addressed in the implementation rather than only in planning.",
            "At Example, event retries caused duplicate alerts. I changed the handling to use durable processing, which resolved the duplicate alerts. I also coordinated four engineers delivering Java services and production releases, while keeping the technical work visible to the rest of the team.",
            "I have also worked on production releases with the same team. Those are the experiences I would bring to conversations about integration failures and release quality. I would be glad to discuss the exact work and where it lines up with the responsibilities of this role.",
        ],
        "evidence_ids": [item["id"] for item in context["public_evidence"]],
        "review_flags": [],
    }
    monkeypatch.setattr(flow.scraper, "sync_application_tracker_if_enabled", lambda: None)

    package = flow.prepare_application_package(
        job["id"], db_path=db, profile_path=profile_path, output_dir=tmp_path / "output",
        render_pdfs=False, include_cover_letter=True, cover_letter_draft=draft,
    )

    cover = json.loads(package.cover_json.read_text(encoding="utf-8"))
    assert cover["paragraphs"] == draft["paragraphs"]
    assert cover["evidence_ids"] == draft["evidence_ids"]
    assert "highlights" not in cover and "opening" not in cover
    assert json.loads(package.manifest_json.read_text(encoding="utf-8"))["cover_letter_included"] is True


def test_failed_regeneration_restores_previous_package_atomically(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    output_dir = tmp_path / "output"
    profile_path = tmp_path / "master-profile.json"
    profile_payload = {
        "name": "Candidate",
        "headline": "Backend Engineer",
        "summary": "Original confirmed summary.",
        "experience": [],
        "education": [],
    }
    profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, "
        "url TEXT, source TEXT, description TEXT, tech_required TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "li-atomic",
            "Backend Engineer",
            "TargetCo",
            "Dubai",
            "https://example.com/job",
            "LinkedIn",
            "Build backend services.",
            "java",
        ),
    )
    conn.commit()
    conn.close()

    original = flow.prepare_application_package(
        "li-atomic",
        db_path=db,
        profile_path=profile_path,
        output_dir=output_dir,
        render_pdfs=False,
    )
    original_resume = original.resume_json.read_bytes()
    original_manifest = original.manifest_json.read_bytes()
    marker = original.package_dir / "previous-package-marker.txt"
    marker.write_text("keep the complete previous package", encoding="utf-8")
    profile_payload["summary"] = "A different confirmed summary for regeneration."
    profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")

    def fail_stage_record(*args, **kwargs):
        raise RuntimeError("database stage update failed")

    monkeypatch.setattr(flow.scraper, "record_application_stage", fail_stage_record)

    with pytest.raises(RuntimeError, match="database stage update failed"):
        flow.prepare_application_package(
            "li-atomic",
            db_path=db,
            profile_path=profile_path,
            output_dir=output_dir,
            render_pdfs=False,
        )

    assert original.resume_json.read_bytes() == original_resume
    assert original.manifest_json.read_bytes() == original_manifest
    assert marker.read_text(encoding="utf-8") == "keep the complete previous package"
    assert [path.name for path in output_dir.iterdir()] == [original.package_dir.name]
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT stage FROM applications WHERE job_id = ?",
        ("li-atomic",),
    ).fetchone()[0] == "package_generated"
    conn.close()


def test_package_generation_rejects_job_id_path_traversal(tmp_path):
    db = tmp_path / "jobs.db"
    output_dir = tmp_path / "output"
    profile_path = tmp_path / "master-profile.json"
    unsafe_job_id = "../victim"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Candidate",
                "headline": "Backend Engineer",
                "experience": [],
                "education": [],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, "
        "url TEXT, source TEXT, description TEXT, tech_required TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            unsafe_job_id,
            "Backend Engineer",
            "TargetCo",
            "Dubai",
            "https://example.com/job",
            "LinkedIn",
            "Build backend services.",
            "java",
        ),
    )
    conn.commit()
    conn.close()
    escaped_target = tmp_path / "victim-targetco-backend-engineer"
    escaped_target.mkdir()
    marker = escaped_target / "keep.txt"
    marker.write_text("must not be replaced", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe path characters"):
        flow.prepare_application_package(
            unsafe_job_id,
            db_path=db,
            profile_path=profile_path,
            output_dir=output_dir,
            render_pdfs=False,
        )

    assert marker.read_text(encoding="utf-8") == "must not be replaced"
    assert not output_dir.exists()


def test_confirmed_variant_is_preserved_and_drives_cover_letter(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    output_dir = tmp_path / "output"
    profile_path = tmp_path / "master-profile.json"
    curated_bullets = [
        "Maintained ten Kotlin and Spring Boot services for a production platform.",
        "Migrated two Java services to Kotlin after the candidate confirmed the wording.",
        "Moved notification delivery to asynchronous batches of 500.",
    ]
    profile = {
        "name": "Candidate",
        "email": "candidate@example.com",
        "headline": "General Software Architect",
        "summary": "General architecture profile.",
        "skills": {"General": ["Azure", "Terraform"]},
        "experience": [
            {
                "title": "Part-time CTO",
                "company": "Excluded Example",
                "dates": "2025 - Present",
                "bullets": ["This role must not leak into the selected application package."],
            }
        ],
        "education": [],
        "additional": {"interests": "Excluded from this one-page variant."},
        "resume_variants": [
            {
                "id": "jvm-backend",
                "confirmation": "candidate-confirmed",
                "match_terms": ["java", "kotlin", "spring boot"],
                "priority": 100,
                "max_pages": 1,
                "omit_sections": ["additional"],
                "resume": {
                    "headline": "Senior Backend Engineer | Java, Kotlin & Spring Boot",
                    "summary": "Backend engineer focused on JVM services.",
                    "skills": {"Backend": ["Java", "Kotlin", "Spring Boot"]},
                    "experience": [
                        {
                            "title": "Senior Backend Engineer",
                            "company": "Curated Example",
                            "dates": "2020 - Present",
                            "bullets": curated_bullets,
                            "tech": "Kotlin - Java - Spring Boot",
                        }
                    ],
                },
            }
        ],
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, "
        "url TEXT, source TEXT, description TEXT, tech_required TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "li-jvm",
            "Software Engineer (Java)",
            "TargetCo",
            "Dubai",
            "https://example.com/job",
            "LinkedIn",
            "Build Java and Spring Boot backend services.",
            "java, spring boot",
        ),
    )
    conn.commit()
    conn.close()

    def fake_render(mode, input_path, output_path):
        output_path.write_bytes(b"%PDF-1.4\n%%EOF")
        return 1

    monkeypatch.setattr(flow, "_render_pdf", fake_render)

    package = flow.prepare_application_package(
        "li-jvm",
        db_path=db,
        profile_path=profile_path,
        output_dir=output_dir,
        render_pdfs=True,
        include_cover_letter=True,
    )

    resume = json.loads(package.resume_json.read_text(encoding="utf-8"))
    cover = json.loads(package.cover_json.read_text(encoding="utf-8"))
    serialized_resume = json.dumps(resume)
    serialized_cover = json.dumps(cover)
    assert resume["name"] == "Candidate"
    assert resume["headline"] == "Senior Backend Engineer | Java, Kotlin & Spring Boot"
    assert resume["experience"][0]["bullets"] == curated_bullets
    assert len(resume["experience"]) == 1
    assert "additional" not in resume
    assert "resume_variants" not in serialized_resume
    assert "Excluded Example" not in serialized_resume
    assert "Excluded Example" not in serialized_cover
    assert "Curated Example" in serialized_cover
    assert any(item["text"] == curated_bullets[0] for item in cover["highlights"])


def test_resume_page_limit_blocks_package_stage_before_cover_render(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    output_dir = tmp_path / "output"
    profile_path = tmp_path / "master-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Candidate",
                "experience": [],
                "education": [],
                "resume_variants": [
                    {
                        "id": "jvm-backend",
                        "confirmation": "candidate-confirmed",
                        "match_terms": ["java"],
                        "max_pages": 1,
                        "resume": {
                            "headline": "Backend Engineer",
                            "experience": [],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, "
        "url TEXT, source TEXT, description TEXT, tech_required TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "li-jvm",
            "Java Engineer",
            "TargetCo",
            "Dubai",
            "https://example.com/job",
            "LinkedIn",
            "Build Java services.",
            "java",
        ),
    )
    conn.commit()
    conn.close()
    rendered_modes = []

    def fake_render(mode, input_path, output_path):
        rendered_modes.append(mode)
        output_path.write_bytes(b"%PDF-1.4\n%%EOF")
        return 2

    monkeypatch.setattr(flow, "_render_pdf", fake_render)

    with pytest.raises(RuntimeError, match="2 pages.*at most 1"):
        flow.prepare_application_package(
            "li-jvm",
            db_path=db,
            profile_path=profile_path,
            output_dir=output_dir,
            render_pdfs=True,
        )

    assert rendered_modes == ["resume"]
    assert not (output_dir / "li-jvm-targetco-java-engineer").exists()
    assert not any(output_dir.iterdir())
    conn = sqlite3.connect(db)
    stage_count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    conn.close()
    assert stage_count == 0

    with pytest.raises(RuntimeError, match="requires PDF rendering"):
        flow.prepare_application_package(
            "li-jvm",
            db_path=db,
            profile_path=profile_path,
            output_dir=output_dir,
            render_pdfs=False,
        )
    conn = sqlite3.connect(db)
    stage_count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    conn.close()
    assert stage_count == 0


def test_load_profile_refuses_to_fabricate_missing_candidate_data(tmp_path):
    with pytest.raises(FileNotFoundError, match="Candidate profile not found"):
        flow._load_profile(tmp_path / "missing-profile.json")

    incomplete = tmp_path / "incomplete-profile.json"
    incomplete.write_text('{"headline":"Backend Engineer"}', encoding="utf-8")
    with pytest.raises(ValueError, match="required name"):
        flow._load_profile(incomplete)


def test_resume_tailoring_only_selects_and_reorders_profile_evidence():
    profile = {
        "name": "Candidate",
        "headline": "Software Architect | Backend",
        "summary": (
            "Backend engineer with 8 years of experience. "
            "Built consumer mobile applications. "
            "Led Java microservices and distributed-system delivery. "
            "Teach Spring Framework."
        ),
        "skills": {
            "Cloud": ["Azure", "AWS"],
            "Backend": ["Spring Security", "Java", "Spring Boot", "RabbitMQ", "Microservices"],
            "Data": ["PostgreSQL", "Redis"],
        },
        "experience": [
            {
                "title": "Senior Software Engineer",
                "company": "Example",
                "bullets": [
                    "Built a marketing landing page.",
                    "Owned Java microservices using RabbitMQ and Redis.",
                    "Improved distributed backend reliability.",
                    "Mentored engineers.",
                    "Maintained office documentation.",
                ],
                "tech": "Java - Spring Boot - RabbitMQ - Redis",
            }
        ],
    }
    job = {
        "title": "Backend Software Engineer",
        "company": "TargetCo",
        "description": "Build Java server-side services using distributed systems, message queues, and cache.",
    }

    resume = flow._tailor_resume(profile, job)

    assert resume["headline"] == profile["headline"]
    assert "TargetCo" not in json.dumps(resume)
    assert resume["skills"]["Backend"][:3] == ["Java", "Spring Boot", "Microservices"]
    original_bullets = set(profile["experience"][0]["bullets"])
    selected_bullets = resume["experience"][0]["bullets"]
    assert set(selected_bullets) <= original_bullets
    assert len(selected_bullets) == 4
    assert "Java microservices" in selected_bullets[0]
    assert resume["experience"][0]["tech"] == profile["experience"][0]["tech"]


def test_resume_tailoring_keeps_current_experience_before_ended_experience():
    profile = {
        "name": "Candidate",
        "headline": "Software Architect",
        "summary": "Software architect with cloud platform experience.",
        "skills": {"Architecture": ["AWS", "Kubernetes", "Terraform"]},
        "experience": [
            {
                "title": "Chief Technology Officer",
                "company": "Side Venture",
                "dates": "2025 - Present",
                "bullets": ["Managed product planning and commercial delivery."],
            },
            {
                "title": "Lead Software Engineer",
                "company": "Consultancy",
                "dates": "2024 - 2026",
                "bullets": [
                    "Designed cloud-native services on AWS with Kubernetes and Terraform.",
                    "Defined API and event-driven architecture decisions.",
                ],
                "engagements": [
                    {
                        "role": "Software Architect",
                        "tech": ["AWS", "Kubernetes", "Terraform"],
                    }
                ],
            },
        ],
        "education": [],
    }
    job = {
        "title": "Software Architect",
        "company": "TargetCo",
        "description": "Design cloud-native SaaS architecture using AWS, Kubernetes, and Terraform.",
    }

    resume = flow._tailor_resume(profile, job)

    assert [item["company"] for item in resume["experience"]] == ["Side Venture", "Consultancy"]
    assert resume["experience"][0]["dates"] == "2025 - Present"
    assert len(resume["experience"]) == len(profile["experience"])


def test_resume_tailoring_ignores_non_public_engagement_metadata_for_ordering():
    profile = {
        "name": "Candidate",
        "experience": [
            {
                "title": "Chief Technology Officer",
                "company": "First Company",
                "dates": "2025 - Present",
                "bullets": [],
            },
            {
                "title": "Lead Software Engineer",
                "company": "Second Company",
                "dates": "2024 - 2026",
                "bullets": [],
                "engagements": [
                    {
                        "role": "Software Architect",
                        "summary": "Unconfirmed private draft architecture work.",
                    }
                ],
            },
        ],
        "education": [],
    }
    job = {
        "title": "Software Architect",
        "description": "Own software architecture decisions.",
    }

    resume = flow._tailor_resume(profile, job)

    assert [item["company"] for item in resume["experience"]] == [
        "First Company",
        "Second Company",
    ]
    assert "engagements" not in json.dumps(resume)


def test_backend_tailoring_keeps_current_lead_first_and_omits_unneeded_cto():
    profile = {
        "name": "Candidate",
        "headline": "Software Architect | Tech Lead",
        "summary": (
            "Software engineer with 7+ years of experience. "
            "CTO and team leader for an advertising platform. "
            "Full-stack engineer on an industrial monitoring platform. "
            "Previously led cloud migration for a client."
        ),
        "experience": [
            {
                "id": "exp-cto", "title": "Chief Technology Officer", "company": "Venture",
                "dates": "October 2025 - Present", "bullets": ["Owned product planning."],
            },
            {
                "id": "exp-lead", "title": "Lead Software Engineer", "company": "Consultancy",
                "dates": "October 2024 - Present",
                "bullets": ["Worked on Azure API Management for a client."],
                "engagements": [{"summary": "PRIVATE_ENGAGEMENT_NOT_FOR_RESUME"}],
            },
            {
                "id": "exp-senior", "title": "Senior Software Engineer", "company": "Consultancy",
                "dates": "October 2022 - October 2024",
                "bullets": ["Built Java microservices for a client."],
            },
        ],
        "evidence_bank": [
            {
                "id": "ev-api", "experience_id": "exp-lead",
                "public_text": "Built a NestJS backend for mobile and web applications.",
                "confirmation": "candidate-confirmed", "confidentiality": "public",
                "visibility": ["resume", "cover-letter"],
            },
            {
                "id": "ev-react", "experience_id": "exp-lead",
                "public_text": "Delivered authorization features across NestJS, React, and shared Zod contracts.",
                "confirmation": "candidate-confirmed", "confidentiality": "public",
                "visibility": ["resume", "cover-letter"],
            },
            {
                "id": "ev-aws", "experience_id": "exp-lead",
                "public_text": "Deployed development and production environments on AWS with PostgreSQL.",
                "confirmation": "candidate-confirmed", "confidentiality": "public",
                "visibility": ["resume", "cover-letter"],
            },
            {
                "id": "ev-private", "experience_id": "exp-lead",
                "public_text": "PRIVATE_EVIDENCE_NOT_FOR_RESUME",
                "confirmation": "candidate-confirmed", "confidentiality": "private",
                "visibility": ["resume", "cover-letter"],
            },
            {
                "id": "ev-unconfirmed", "experience_id": "exp-lead",
                "public_text": "UNCONFIRMED_MODEL_DELIVERY",
                "confirmation": "unconfirmed", "confidentiality": "public",
                "visibility": ["resume", "cover-letter"],
            },
        ],
    }
    job = {
        "title": "Senior Backend Engineer",
        "company": "TargetCo",
        "description": "Build and operate NodeJS and TypeScript services on AWS; React experience is useful.",
    }

    resume = flow._tailor_resume(profile, job)
    lead = resume["experience"][0]

    assert lead["title"] == "Lead Software Engineer"
    assert lead["company"] == "Consultancy"
    assert lead["dates"] == "October 2024 - Present"
    assert set(lead["bullets"][:3]) == {
        "Delivered authorization features across NestJS, React, and shared Zod contracts.",
        "Deployed development and production environments on AWS with PostgreSQL.",
        "Built a NestJS backend for mobile and web applications.",
    }
    assert resume["headline"] == profile["headline"]
    assert [item["title"] for item in resume["experience"]] == [
        "Lead Software Engineer", "Senior Software Engineer"
    ]
    assert {item["dates"] for item in resume["experience"]} == {
        "October 2024 - Present", "October 2022 - October 2024"
    }
    serialized = json.dumps(resume)
    cover_serialized = json.dumps(flow._cover_letter(profile, job))
    assert "PRIVATE_" not in serialized
    assert "UNCONFIRMED_" not in serialized
    assert "engagements" not in serialized
    assert "PRIVATE_" not in cover_serialized
    assert "UNCONFIRMED_" not in cover_serialized


def test_staff_role_for_former_engineering_leaders_keeps_public_cto_experience():
    profile = {
        "name": "Candidate",
        "summary": (
            "Software engineer with seven years of experience. "
            "CTO and team leader for a product company. "
            "Built backend services on AWS. "
            "Delivered web features with React."
        ),
        "experience": [
            {
                "id": "exp-cto", "title": "Chief Technology Officer", "company": "Venture",
                "dates": "October 2025 - Present", "bullets": ["Led product engineering."],
            },
            {
                "id": "exp-lead", "title": "Lead Software Engineer", "company": "Consultancy",
                "dates": "October 2024 - Present", "bullets": ["Built backend services on AWS."],
            },
            {
                "id": "exp-senior", "title": "Senior Software Engineer", "company": "Consultancy",
                "dates": "October 2022 - October 2024", "bullets": ["Built Java services."],
            },
        ],
        "evidence_bank": [
            {
                "id": "ev-cto", "experience_id": "exp-cto",
                "public_text": "Owned technical delivery for a small engineering team.",
                "confirmation": "candidate-confirmed", "confidentiality": "public",
                "visibility": ["resume"],
            },
            {
                "id": "ev-private", "experience_id": "exp-cto",
                "public_text": "PRIVATE_CTO_DETAIL",
                "confirmation": "candidate-confirmed", "confidentiality": "private",
                "visibility": ["resume"],
            },
            {
                "id": "ev-unconfirmed", "experience_id": "exp-cto",
                "public_text": "UNCONFIRMED_CTO_RESULT",
                "confirmation": "unconfirmed", "confidentiality": "public",
                "visibility": ["resume"],
            },
        ],
    }
    job = {
        "title": "Staff Software Engineer (UAE)",
        "description": (
            "This role is designed for former engineering leaders (IC or EM) or founders "
            "who are comfortable owning end-to-end technical outcomes but specifically "
            "want to continue being impactful as individual contributors and spend more "
            "time in the code."
        ),
    }

    resume = flow._tailor_resume(profile, job)

    assert [item["company"] for item in resume["experience"]] == [
        "Venture", "Consultancy", "Consultancy",
    ]
    assert resume["experience"][0]["dates"] == "October 2025 - Present"
    assert "Owned technical delivery for a small engineering team." in resume["experience"][0]["bullets"]
    assert "CTO and team leader for a product company." in resume["summary"]
    assert "PRIVATE_" not in json.dumps(resume)
    assert "UNCONFIRMED_" not in json.dumps(resume)


@pytest.mark.parametrize("title, description", [
    ("Staff Software Engineer", "Collaborate with former engineering leaders and founders."),
    ("Staff Software Engineer", "This role is designed for backend engineers at a founder-led company."),
    ("Senior Backend Engineer", "This role is designed for former engineering leaders or founders."),
])
def test_cto_role_is_omitted_without_staff_leadership_candidate_intent(title, description):
    profile = {
        "name": "Candidate",
        "experience": [
            {"title": "Chief Technology Officer", "company": "Venture",
             "dates": "October 2025 - Present", "bullets": ["Led product engineering."]},
            {"title": "Lead Software Engineer", "company": "Consultancy",
             "dates": "October 2024 - Present", "bullets": ["Built backend services."]},
        ],
    }

    resume = flow._tailor_resume(profile, {"title": title, "description": description})

    assert [item["company"] for item in resume["experience"]] == ["Consultancy"]


def test_optional_older_role_needs_direct_stack_match_and_current_roles_stay_chronological():
    profile = {
        "name": "Candidate",
        "experience": [
            {"title": "Software Engineer", "company": "Older Employer",
             "dates": "February 2019 - September 2020",
             "bullets": ["Built Java and Spring Boot services with RabbitMQ."],
             "tech": "Java | Spring Boot | RabbitMQ"},
            {"title": "Senior Software Engineer", "company": "Consultancy",
             "dates": "October 2022 - October 2024", "bullets": ["Built backend services."]},
            {"title": "Lead Software Engineer", "company": "Consultancy",
             "dates": "October 2024 - Present", "bullets": ["Built AWS and NestJS services."]},
            {"title": "Chief Technology Officer", "company": "Side Venture",
             "dates": "October 2025 - Present", "bullets": ["Led product delivery."]},
        ],
    }

    manager = flow._tailor_resume(profile, {
        "title": "Engineering Manager", "description": "Lead delivery teams using AWS."
    })
    java_backend = flow._tailor_resume(profile, {
        "title": "Senior Backend Engineer",
        "description": "Build Java and Spring Boot services with RabbitMQ.",
    })

    assert [item["company"] for item in manager["experience"]] == [
        "Side Venture", "Consultancy", "Consultancy"
    ]
    assert [item["company"] for item in java_backend["experience"]] == [
        "Consultancy", "Consultancy", "Older Employer"
    ]


def test_backend_summary_uses_personal_strengths_when_posting_requests_mentorship():
    profile = {
        "name": "Candidate",
        "summary": (
            "Backend engineering leader with 8 years in software engineering. "
            "Managed a team of four engineers while remaining hands-on with microservices. "
            "Built production services on AWS using TypeScript, PostgreSQL, and Kubernetes. "
            "Delivered full-stack features with React."
        ),
        "experience": [{"title": "Lead Software Engineer", "company": "Example",
                        "dates": "2024 - Present", "bullets": ["Built backend services."]}],
    }
    job = {
        "title": "Senior Backend Engineer",
        "description": "Build TypeScript services on AWS, mentor engineers, and own production reliability.",
    }

    summary = flow._tailor_resume(profile, job)["summary"]

    assert summary.startswith("Backend engineering leader with 8 years")
    assert "Managed a team of four" in summary
    assert "Built production services on AWS" in summary
    assert "Delivered full-stack features" not in summary


def test_confirmed_variant_experiences_still_follow_candidate_chronology_rule():
    profile = {
        "name": "Candidate",
        "experience": [],
        "resume_variants": [{
            "id": "platform-variant",
            "confirmation": "candidate-confirmed",
            "role_terms": ["platform architect"],
            "match_terms": ["developer platform"],
            "resume": {"experience": [
                {"title": "Senior Software Engineer", "company": "Example",
                 "dates": "2022 - 2024", "bullets": ["Built services."]},
                {"title": "Lead Software Engineer", "company": "Example",
                 "dates": "2024 - Present", "bullets": ["Led delivery."]},
            ]},
        }],
    }

    resume, selected = flow._resume_for_job(profile, {
        "title": "Platform Architect", "description": "Build a developer platform."
    })

    assert selected["id"] == "platform-variant"
    assert [item["title"] for item in resume["experience"]] == [
        "Lead Software Engineer", "Senior Software Engineer"
    ]


def test_manager_and_backend_fallbacks_use_distinct_confirmed_evidence():
    profile = {
        "name": "Candidate",
        "headline": "Software Architect | Tech Lead",
        "summary": (
            "Software engineer with 7+ years of experience. "
            "CTO and team leader for an advertising platform. "
            "Full-stack engineer on an industrial monitoring platform. "
            "Previously led cloud migration and engineering teams."
        ),
        "skills": {"Backend": ["TypeScript", "AWS", "LLM integration", "Team management"]},
        "experience": [
            {
                "id": "exp-cto", "title": "Chief Technology Officer", "company": "Venture",
                "dates": "2025 - Present", "bullets": ["Owned architecture for enterprise clients."],
            },
            {
                "id": "exp-lead", "title": "Lead Software Engineer", "company": "Consultancy",
                "dates": "2024 - Present", "bullets": ["Built a NestJS backend for mobile applications."],
            },
            {
                "id": "exp-senior", "title": "Senior Software Engineer", "company": "Consultancy",
                "dates": "2022 - 2024",
                "bullets": [
                    "Managed a team of 4 engineers delivering a client platform.",
                    "Coordinated a team of four engineers while delivering the client platform.",
                    "Mentored engineers on deployment practices.",
                ],
            },
        ],
        "evidence_bank": [
            {
                "id": "ev-workflow", "experience_id": "exp-lead",
                "public_text": "Designed the team's spec workflow with CI checks and agent skills.",
                "confirmation": "candidate-confirmed", "confidentiality": "public",
                "visibility": ["resume", "cover-letter"],
            },
            {
                "id": "ev-fullstack", "experience_id": "exp-lead",
                "public_text": "Delivered authorization features across NestJS, React, and Zod.",
                "confirmation": "candidate-confirmed", "confidentiality": "public",
                "visibility": ["resume", "cover-letter"],
            },
        ],
    }
    manager_job = {
        "title": "Engineering Manager", "company": "PublicAI",
        "description": "Lead engineers delivering full stack AI applications to government clients and mentor the team. "
                       "Production machine learning model training is required.",
    }
    backend_job = {
        "title": "Senior Backend Engineer", "company": "CallCo",
        "description": "Build NodeJS and TypeScript backend services on AWS with React integrations.",
    }

    manager = flow._tailor_resume(profile, manager_job)
    backend = flow._tailor_resume(profile, backend_job)
    manager_bullets = [bullet for item in manager["experience"] for bullet in item["bullets"]]
    manager_letter = flow._cover_letter(profile, manager_job)
    backend_letter = flow._cover_letter(profile, backend_job)

    assert manager["summary"] != backend["summary"]
    assert "CTO and team leader" in manager["summary"]
    assert "Full-stack engineer" in backend["summary"]
    assert any("spec workflow" in bullet for bullet in manager_bullets)
    assert any("NestJS, React" in bullet for bullet in manager_bullets)
    assert sum("team of 4 engineers" in bullet or "team of four engineers" in bullet for bullet in manager_bullets) == 1
    assert "government clients" in manager_letter["opening"]
    assert "TypeScript and Node.js services" in backend_letter["opening"]
    assert "production machine learning" not in manager_letter["opening"].lower()
    assert "LLM integration" not in manager_letter["opening"]
    assert "Azure" not in backend_letter["opening"]


def test_role_skills_and_communications_reliability_evidence_lead_backend_resume():
    webhook_fix = (
        "Eliminated duplicate customer notifications caused by third-party webhook retries "
        "by acknowledging requests and processing them asynchronously."
    )
    profile = {
        "name": "Candidate",
        "summary": "Backend engineer operating production services.",
        "skills": {
            "Data & Languages": ["TypeScript", "PostgreSQL"],
            "Leadership": ["Mentoring"],
            "Backend & Architecture": ["Spring Boot", "NestJS", "REST APIs"],
            "Cloud": ["Azure", "AWS"],
        },
        "experience": [
            {
                "title": "Lead Software Engineer", "company": "Example", "dates": "2024 - Present",
                "bullets": ["Built production NestJS services on AWS."],
                "tech": ".NET/C# · Cosmos DB · AWS · NestJS",
            },
            {
                "title": "Senior Software Engineer", "company": "Example", "dates": "2022 - 2024",
                "bullets": [
                    "Managed a team of engineers.",
                    "Improved CI/CD workflows for releases.",
                    "Built Java services on AWS.",
                    "Migrated Spring Boot services.",
                    webhook_fix,
                ],
            },
        ],
    }
    job = {
        "title": "Senior Backend Engineer, Customer Communications Platform",
        "company": "CallCo",
        "description": "Build TypeScript and Node.js services on AWS with high availability for customer communications.",
    }

    resume = flow._tailor_resume(profile, job)
    senior = next(item for item in resume["experience"] if item["title"] == "Senior Software Engineer")

    assert next(iter(resume["skills"])) == "Backend & Architecture"
    assert resume["skills"]["Backend & Architecture"][0] == "NestJS"
    assert resume["skills"]["Data & Languages"][0] == "TypeScript"
    assert resume["skills"]["Cloud"][0] == "AWS"
    assert resume["experience"][0]["tech"] == "AWS · NestJS"
    assert webhook_fix in senior["bullets"]
    letter = flow._cover_letter(profile, job)
    assert webhook_fix in [item["text"] for item in letter["highlights"]]


def test_same_employer_roles_group_under_one_tenure_with_dated_engagements():
    experiences = [
        {"title": "Chief Technology Officer", "company": "SideCo", "dates": "October 2025 - Present",
         "bullets": ["Led engineering."]},
        {"title": "Lead Software Engineer", "company": "Example GmbH", "dates": "October 2024 - Present",
         "bullets": ["Built NestJS services on AWS."]},
        {"title": "Senior Software Engineer", "company": "Example GmbH",
         "dates": "October 2022 - October 2024", "bullets": ["Built Java microservices."]},
    ]
    profile = {"employment_groups": [{
        "company": "Example GmbH", "confirmation": "candidate-reviewed",
        "title": "Lead Software Engineer",
        "dates": "October 2020 - Present", "progression": "Promoted twice.",
        "engagements": [
            {"name": "Older client", "dates": "October 2020 - October 2024",
             "bullets": ["Built Java microservices."], "tech": "Java"},
            {"name": "New client", "dates": "August 2026 - Present",
             "bullets": ["Built NestJS services on AWS."], "tech": "NestJS, AWS"},
        ],
    }]}
    job_text = flow._normalized_relevance_text("Backend engineer using NestJS, AWS and Java microservices")

    grouped = flow._group_company_experiences(experiences, profile, job_text, "Backend Engineer")

    assert [item["company"] for item in grouped] == ["SideCo", "Example GmbH"]
    employer = grouped[1]
    assert employer["dates"] == "October 2020 - Present"
    assert employer["progression"] == "Promoted twice."
    assert [item["name"] for item in employer["engagements"]] == ["New client", "Older client"]
    assert employer["bullets"] == ["Built NestJS services on AWS.", "Built Java microservices."]


def test_maibornwolff_is_presented_before_newer_cto_role():
    experiences = [
        {"title": "Chief Technology Officer", "company": "VERSE",
         "dates": "October 2025 - Present", "bullets": ["Led engineering."]},
        {"title": "Lead Software Engineer", "company": "MaibornWolff GmbH",
         "dates": "October 2024 - Present", "bullets": ["Built services."]},
        {"title": "Senior Software Engineer", "company": "MaibornWolff GmbH",
         "dates": "October 2022 - October 2024", "bullets": ["Built microservices."]},
    ]
    profile = {"employment_groups": [{
        "company": "MaibornWolff GmbH", "confirmation": "candidate-reviewed",
        "title": "Lead Software Engineer", "dates": "October 2020 - Present",
        "engagements": [{"name": "Client", "dates": "August 2026 - Present",
                         "bullets": ["Built services."]}],
    }]}

    grouped = flow._group_company_experiences(
        experiences, profile, "backend architecture", "Platform Architect",
    )

    assert [item["company"] for item in grouped] == ["MaibornWolff GmbH", "VERSE"]
    assert grouped[0]["dates"] == "October 2020 - Present"
    assert grouped[0]["engagements"][0]["dates"] == "August 2026 - Present"


def test_single_confirmed_client_role_uses_full_maibornwolff_tenure():
    experiences = [{
        "title": "Senior Software Engineer", "company": "MaibornWolff GmbH",
        "subtitle": "Rolls-Royce Whispers", "dates": "October 2020 - October 2024",
        "bullets": ["Built the Whispers backend."],
    }]
    profile = {"employment_groups": [{
        "company": "MaibornWolff GmbH", "confirmation": "candidate-reviewed",
        "title": "Lead Software Engineer", "dates": "October 2020 - Present",
        "engagements": [{
            "name": "Rolls-Royce Whispers", "dates": "October 2020 - October 2024",
            "aliases": ["Whispers"], "bullets": ["Built the Whispers backend."],
        }],
    }]}

    grouped = flow._group_company_experiences(
        experiences, profile, "backend", "Senior Backend Engineer",
        preserve_variant=True,
    )

    assert grouped[0]["dates"] == "October 2020 - Present"
    assert grouped[0]["engagements"][0]["name"] == "Rolls-Royce Whispers"
    assert grouped[0]["engagements"][0]["dates"] == "October 2020 - October 2024"


def test_grouped_confirmed_variant_keeps_approved_bullet_wording():
    experiences = [
        {"title": "Lead Software Engineer", "company": "Example GmbH",
         "dates": "October 2024 - Present", "bullets": ["Exact approved lead wording."]},
        {"title": "Senior Software Engineer", "company": "Example GmbH",
         "dates": "October 2022 - October 2024", "bullets": ["Exact approved senior wording."]},
    ]
    profile = {"employment_groups": [{"company": "Example GmbH", "confirmation": "candidate-reviewed",
                                     "title": "Lead Software Engineer",
                                     "dates": "October 2020 - Present", "engagements": []}]}

    grouped = flow._group_company_experiences(
        experiences, profile, "", "Platform Architect", preserve_variant=True,
    )

    assert [child["name"] for child in grouped[0]["engagements"]] == [
        "Lead Software Engineer", "Senior Software Engineer",
    ]
    assert grouped[0]["bullets"] == ["Exact approved lead wording.", "Exact approved senior wording."]


def test_grouped_confirmed_variant_uses_client_dates_for_identifiable_work():
    experiences = [
        {"title": "Lead Software Engineer", "company": "Example GmbH",
         "dates": "October 2024 - Present",
         "bullets": ["Supported PlaTo migration.", "Contributed to Husky systems."]},
        {"title": "Senior Software Engineer", "company": "Example GmbH",
         "dates": "October 2022 - October 2024",
         "bullets": ["Owned ten Spring Boot microservices."]},
    ]
    profile = {"employment_groups": [{
        "company": "Example GmbH", "confirmation": "candidate-reviewed",
        "title": "Lead Software Engineer", "dates": "October 2020 - Present",
        "engagements": [
            {"name": "Rolls-Royce Whispers", "dates": "October 2020 - October 2024",
             "aliases": ["Rolls-Royce", "Whispers"], "tech": "Spring Boot"},
            {"name": "Husky", "dates": "March 2025 - July 2025",
             "aliases": ["Husky"], "tech": "Azure"},
            {"name": "PlaTo / MO360", "dates": "October 2025 - December 2025",
             "aliases": ["PlaTo", "MO360"], "tech": "Azure"},
        ],
    }]}

    grouped = flow._group_company_experiences(
        experiences, profile, "platform architecture", "Platform Architect",
        preserve_variant=True,
    )

    engagements = grouped[0]["engagements"]
    assert [item["name"] for item in engagements] == [
        "PlaTo / MO360", "Husky", "Rolls-Royce Whispers",
    ]
    assert [item["bullets"] for item in engagements] == [
        ["Supported PlaTo migration."],
        ["Contributed to Husky systems."],
        ["Owned ten Spring Boot microservices."],
    ]


def test_unconfirmed_employer_group_is_not_rendered_as_public_history():
    experiences = [
        {"title": "Lead Engineer", "company": "Example GmbH", "dates": "2024 - Present"},
        {"title": "Engineer", "company": "Example GmbH", "dates": "2020 - 2024"},
    ]
    profile = {"employment_groups": [{
        "company": "Example GmbH", "dates": "2020 - Present",
        "engagements": [{"name": "Unconfirmed client", "dates": "2024 - Present"}],
    }]}

    assert flow._group_company_experiences(experiences, profile, "", "Engineer") == experiences


def test_resume_bullet_ranking_prefers_distinct_confirmed_work_when_scores_are_close():
    deployment = "Deployed separate development and production services on AWS with PostgreSQL and Docker."
    feature = "Delivered authorization and history features across NestJS, React, and TypeScript."
    repeated = "Designed architecture using AWS, NestJS, and Terraform."
    workflow = "Designed an OpenSpec workflow with Python validators, Git hooks, and CI checks."
    bullets = [
        deployment,
        "Built a NestJS backend for a mobile application.",
        feature,
        repeated,
        workflow,
    ]
    job_text = flow._normalized_relevance_text(
        "Senior Backend Engineer building NodeJS TypeScript services on AWS with PostgreSQL "
        "and React integrations, CI delivery workflows, reliability, Python validators, "
        "and Git hooks for workflow checks"
    )

    selected = flow._ranked_distinct_bullets(bullets, job_text, 3, "Senior Backend Engineer")

    assert selected == [deployment, feature, workflow]
    assert repeated not in selected


def test_cover_uses_approved_website_contact_without_repeated_discussion_paragraph():
    profile = {
        "name": "Candidate",
        "website": "https://candidate.example/",
        "email": "candidate@example.com",
        "phone": "+216 12 345 678",
        "linkedin": "linkedin.com/in/candidate",
        "experience": [],
    }
    letter = flow._cover_letter(profile, sample_job())

    assert letter["contact"] == "https://candidate.example/ | candidate@example.com | linkedin.com/in/candidate"
    assert "motivation" not in letter
    assert letter["closing"].count("discuss") == 1


def test_aiqu_architect_package_blocks_legacy_fallback_without_advancing_stage(tmp_path):
    db = tmp_path / "jobs.db"
    profile_path = tmp_path / "master-profile.json"
    output_dir = tmp_path / "output"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Candidate",
                "headline": "Software Architect",
                "summary": "General architecture profile.",
                "experience": [],
                "education": [],
                "resume_variants": [
                    {
                        "id": "jvm-backend",
                        "confirmation": "candidate-confirmed",
                        "match_terms": ["java", "spring boot"],
                        "resume": {"headline": "Senior Backend Engineer"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, "
        "url TEXT, source TEXT, description TEXT, tech_required TEXT)"
    )
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "li-4440689544",
            "Software Architect",
            "AIQU",
            "Dubai",
            "https://example.com/aiqu",
            "LinkedIn",
            "Design cloud-native Java SaaS platforms with Kubernetes, Terraform, GitOps, and zero-trust security.",
            "java, kubernetes, terraform, gitops, zero-trust",
        ),
    )
    conn.commit()
    conn.close()

    with pytest.raises(flow.TailoringReadinessError, match="Software Architect resume variant"):
        flow.prepare_application_package(
            "li-4440689544",
            db_path=db,
            profile_path=profile_path,
            output_dir=output_dir,
            render_pdfs=False,
        )

    assert not output_dir.exists()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    ("senior_dates", "lead_dates"),
    [
        ("October 2022 - Present", "November 2024 - May 2026"),
        ("Oct 2022 - Present", "Nov 2024 - May 2026"),
        ("2022 - Present", "2024 - 2026"),
    ],
)
def test_same_employer_progression_marked_present_blocks_package_generation(
    senior_dates,
    lead_dates,
):
    profile = {
        "name": "Candidate",
        "experience": [
            {
                "title": "Lead Software Engineer",
                "company": "Example",
                "dates": lead_dates,
                "bullets": [],
            },
            {
                "title": "Senior Software Engineer",
                "company": "Example",
                "dates": senior_dates,
                "bullets": [],
            },
        ],
    }

    with pytest.raises(flow.TailoringReadinessError, match="inconsistent same-employer role progression"):
        flow._assert_tailoring_ready(
            profile,
            {"title": "Backend Engineer"},
            selected_variant=None,
        )


def test_aiqu_architect_role_variant_wins_and_preserves_approved_order():
    approved_order = ["Architecture Engagement", "Senior Software Engineer", "CTO"]
    profile = {
        "name": "Candidate",
        "headline": "General profile",
        "experience": [],
        "resume_variants": [
            {
                "id": "software-architect-approved",
                "confirmation": "candidate-confirmed",
                "role_terms": ["software architect", "architecture engineer"],
                "match_terms": ["cloud-native", "kubernetes", "terraform"],
                "max_pages": 2,
                "resume": {
                    "headline": "Software Architect | Cloud-Native Platforms",
                    "summary": "Approved architecture summary.",
                    "experience": [
                        {
                            "title": title,
                            "company": "Approved Company",
                            "dates": "Approved dates",
                            "bullets": ["Approved evidence."],
                        }
                        for title in approved_order
                    ],
                },
            }
        ],
    }
    job = {
        "title": "Software Architect",
        "description": "Cloud-native SaaS on Kubernetes with Terraform.",
        "tech_required": "kubernetes, terraform",
    }

    resume, variant = flow._resume_for_job(profile, job)

    assert variant["id"] == "software-architect-approved"
    assert [item["title"] for item in resume["experience"]] == approved_order
    assert resume["summary"] == "Approved architecture summary."


def test_cover_letter_is_specific_complete_and_evidence_based():
    profile = {
        "name": "Candidate",
        "email": "candidate@example.com",
        "headline": "Backend Engineer",
        "summary": "Backend engineer with 8+ years of experience.",
        "skills": {
            "Backend": ["Java", "Spring Boot", "Microservices", "RabbitMQ"],
            "Data": ["PostgreSQL", "Redis"],
        },
        "experience": [
            {
                "title": "Senior Software Engineer",
                "company": "Example",
                "bullets": [
                    "Owned Java microservices using RabbitMQ and Redis.",
                    "Improved distributed backend reliability.",
                    "Led production delivery and monitoring.",
                ],
            }
        ],
    }
    job = {
        "title": "Backend Software Engineer, Office Systems",
        "company": "TargetCo",
        "description": "Build scalable server-side products, distributed systems, and backend infrastructure using Java.",
    }

    letter = flow._cover_letter(profile, job)
    letter_text = json.dumps(letter, ensure_ascii=False).lower()

    assert "targetco" in letter_text
    assert "backend software engineer, office systems" in letter_text
    assert letter["salutation"] == "Dear Hiring Team,"
    assert letter["signoff"] == "Sincerely,"
    assert letter["signature"] == "Candidate"
    assert {highlight["text"] for highlight in letter["highlights"]} == set(profile["experience"][0]["bullets"])
    assert "compensation" not in letter_text
    assert "tailored" not in letter_text
    assert "generated" not in letter_text


def test_cover_draft_context_excludes_private_evidence_and_validates_agent_prose():
    profile = {
        "name": "Candidate",
        "headline": "Backend Engineer",
        "summary": "Backend engineer with confirmed production work.",
        "experience": [{
            "id": "exp-one", "title": "Senior Engineer", "company": "Example", "dates": "2022 - Present",
            "bullets": [
                "Resolved duplicate alerts caused by event retries through durable processing.",
                "Led four engineers delivering Java services and production releases.",
            ],
        }],
        "evidence_bank": [
            {"id": "fact-public", "experience_id": "exp-one",
             "public_text": "Migrated three production services without downtime.",
             "confirmation": "candidate-confirmed", "confidentiality": "public",
             "visibility": ["resume", "cover-letter"]},
            {"id": "fact-private", "experience_id": "exp-one",
             "public_text": "PRIVATE INTERNAL RESULT", "confirmation": "candidate-confirmed",
             "confidentiality": "private", "visibility": ["interview-only"]},
        ],
    }
    job = {"id": "job-one", "title": "Java Technical Lead", "company": "TargetCo",
           "description": "Lead Java services and investigate production integration failures."}
    context = flow.cover_letter_draft_context(profile, job)
    serialized = json.dumps(context)
    assert "PRIVATE INTERNAL RESULT" not in serialized
    assert len(context["public_evidence"]) == 3
    draft = {
        "paragraphs": [
            "Your technical lead role asks for someone who can stay close to backend delivery while a team handles difficult integrations. That combination matches the work I have done on production Java services, where reliability had to be addressed in the implementation rather than only in planning.",
            "At Example, event retries caused duplicate alerts. I changed the handling to use durable processing, which resolved the duplicate alerts. I also coordinated four engineers delivering Java services and production releases, while keeping the technical work visible to the rest of the team.",
            "I have also migrated three production services without downtime. Those are the experiences I would bring to conversations about integration failures and release quality. I would be glad to discuss the exact work and where it lines up with the responsibilities of this role.",
        ],
        "evidence_ids": [item["id"] for item in context["public_evidence"]],
        "review_flags": ["Check requested team size against confirmed experience."],
    }
    assert flow.validate_cover_letter_draft(draft, context)["paragraphs"] == draft["paragraphs"]
    with pytest.raises(flow.TailoringReadinessError, match="unavailable public evidence"):
        flow.validate_cover_letter_draft({**draft, "evidence_ids": ["E999", "E1"]}, context)
    with pytest.raises(flow.TailoringReadinessError, match="boilerplate"):
        flow.validate_cover_letter_draft({**draft, "paragraphs": [
            "I am excited to apply. " + draft["paragraphs"][0], *draft["paragraphs"][1:]
        ]}, context)


def test_confirmed_variant_cover_letter_preserves_approved_experience_order():
    approved_architect_bullets = [
        "Designed the approved architecture.",
        "Deployed the approved cloud environments.",
        "Implemented the approved audit trail.",
    ]
    profile = {
        "name": "Candidate",
        "headline": "Software Architect",
        "summary": "Approved architect summary.",
        "skills": {"Architecture": ["AWS", "Terraform"]},
        "experience": [
            {
                "title": "Software Architect - Client Project",
                "company": "Consultancy",
                "dates": "2026",
                "bullets": approved_architect_bullets,
            },
            {
                "title": "Chief Technology Officer",
                "company": "Venture",
                "dates": "2025 - Present",
                "bullets": [
                    "Owned architecture, infrastructure, reliability, security, and platform delivery."
                ],
            },
        ],
        "education": [],
    }
    job = {
        "title": "Software Architect",
        "company": "TargetCo",
        "description": "Own cloud architecture, security, and platform reliability.",
    }

    letter = flow._cover_letter(profile, job, preserve_experience_order=True)

    assert [item["text"] for item in letter["highlights"]] == approved_architect_bullets
    assert all("Software Architect" in item["context"] for item in letter["highlights"])


def test_cover_renderer_keeps_short_letter_to_one_page(tmp_path):
    cover = pdf_renderer.CoverLetterPDF(
        {
            "name": "Candidate",
            "contact": "candidate@example.com",
            "date": "July 13, 2026",
            "recipient": "TargetCo Hiring Team",
            "subject": "Application for Backend Engineer",
            "salutation": "Dear Hiring Team,",
            "opening": "I am applying for the Backend Engineer role at TargetCo.",
            "highlights_heading": "Relevant examples from my experience include:",
            "highlights": [
                {"text": "Owned Java microservices using RabbitMQ and Redis.", "context": "Senior Engineer - Example"},
                {"text": "Improved distributed backend reliability.", "context": "Senior Engineer - Example"},
            ],
            "motivation": "I am interested in the team's backend infrastructure work.",
            "closing": "Thank you for your consideration.",
            "signoff": "Sincerely,",
            "signature": "Candidate",
        }
    )
    cover.render()
    cover_path = tmp_path / "cover.pdf"
    cover.output(cover_path)

    assert len(cover.pages) == 1
    assert cover_path.stat().st_size > 1000


def test_application_package_defaults_are_project_anchored():
    project_dir = Path(flow.__file__).resolve().parent

    assert flow.DEFAULT_DB_PATH == project_dir / "data" / "jobs.db"
    assert flow.DEFAULT_PROFILE_PATH == project_dir / "data" / "master-profile.json"
    assert flow.DEFAULT_OUTPUT_DIR == project_dir / "data" / "output"


def test_package_ready_keyboard_requires_final_apply_approval():
    keyboard = flow.package_ready_keyboard("li-1")
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]

    assert {button["text"] for button in buttons} >= {"🚀 Proceed to apply", "⏸ Pause"}
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} >= {"proceed_apply:li-1", "pause:li-1"}


def test_research_dry_run_reads_job_without_creating_state_tables(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, salary TEXT, url TEXT)")
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
        ("li-1", "Solutions Architect", "TrueForge", "Dubai", "", "https://example.com/job"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        flow,
        "research_job",
        lambda job: flow.JobResearch(company_summary="", legitimacy=""),
    )

    message = flow.render_research_dry_run("li-1", db_path=db)

    assert "No published range; no TrueForge pay data" in message
    conn = sqlite3.connect(db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"jobs"}
