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
    assert "Fixed monthly salary and bonus/equity terms" in message
    assert len(message) < 600


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
    assert "Fixed monthly salary and bonus/equity terms" in message


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


def test_prepare_application_package_creates_resume_cover_and_records_stage(tmp_path, monkeypatch):
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

    package = flow.prepare_application_package(
        "li-1",
        db_path=db,
        profile_path=profile,
        output_dir=output_dir,
        render_pdfs=False,
    )

    assert package.package_dir.exists()
    assert package.resume_json.exists()
    assert package.cover_json.exists()
    assert package.resume_pdf.name == "Resume.pdf"
    assert package.cover_pdf.name == "CoverLetter.pdf"
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
    assert len(selected_bullets) == 3
    assert "Java microservices" in selected_bullets[0]
    assert resume["experience"][0]["tech"] == profile["experience"][0]["tech"]


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


def test_pdf_renderers_keep_cover_to_one_page_and_prevent_orphaned_blocks(tmp_path):
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

    resume = pdf_renderer.ResumePDF({"name": "Candidate"})
    resume.add_page()
    resume.set_y(resume.h - resume.b_margin - 5)
    resume.ensure_space(20)
    assert resume.page_no() == 2


def test_application_package_defaults_are_project_anchored():
    project_dir = Path(flow.__file__).resolve().parent

    assert flow.DEFAULT_DB_PATH == project_dir / "data" / "jobs.db"
    assert flow.DEFAULT_PROFILE_PATH == project_dir / "data" / "master-profile.json"
    assert flow.DEFAULT_OUTPUT_DIR == project_dir / "data" / "output"


def test_package_ready_keyboard_requires_final_apply_approval():
    keyboard = flow.package_ready_keyboard("li-1")
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]

    assert {button["text"] for button in buttons} >= {"🚀 Proceed to apply", "⏸ Pause"}
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} >= {"proceed_apply:li-1", "ignore:li-1"}


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
